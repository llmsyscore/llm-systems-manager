// ===========================================================================
// Benchmark (llama-bench)
// ===========================================================================
let _benchEventSrc      = null;
let _benchChart         = null;
let _benchData          = {};   // model_id → stored results (DB)
let _benchSwitches      = [];   // current editable switch list
let _benchModelDatasets = {};   // model_id → base index of its [ppt, gen, pg] dataset triple
let _benchRawRows       = [];   // all result rows for axis re-render: {model_id, ts, seq, gen_tps, ppt_tps, n_prompt, n_gen, n_depth, n_batch, n_ubatch, avg_ts}
let _benchAxisTouched   = false; // true once the user picks an axis; until then dropdowns honor the computed default (n_depth / avg_ts)

const BENCH_SERIES_COLORS = [
  {gen: '#5a8fc2', ppt: '#c28a3a', pg: '#5ac27a'},   // steel blue / warm amber / mint
  {gen: '#a05ac2', ppt: '#3aaa7a', pg: '#c2b03a'},   // muted purple / teal / gold
  {gen: '#c25a6a', ppt: '#5aaac2', pg: '#7ac23a'},   // dusty rose / sky / lime
  {gen: '#7a9a3a', ppt: '#c27a3a', pg: '#3a9ac2'},   // olive / terra / azure
  {gen: '#3a7ac2', ppt: '#c25a9a', pg: '#3ac28a'},   // cobalt / mauve / seafoam
];

// Offset of a result row within its model's [ppt, gen, pg] dataset triple.
const _BENCH_SERIES_NAMES = ['ppt', 'gen', 'pg'];
function _benchSeriesOffset(nP, nG) {
  if (nP > 0 && nG > 0) return 2;
  if (nP > 0) return 0;
  if (nG > 0) return 1;
  return null;
}

// Orders the category X axis ascending (numeric when every label is numeric).
function _benchSyncChartLabels() {
  if (!_benchChart) return;
  const xs = [...new Set(_benchChart.data.datasets.flatMap(d => d.data.map(p => p.x)))];
  const numeric = xs.length > 0 && xs.every(v => v !== '' && !isNaN(Number(v)));
  xs.sort(numeric ? (a, b) => Number(a) - Number(b) : (a, b) => String(a).localeCompare(String(b)));
  _benchChart.data.labels = xs;
}

// Row → triple offset: explicit series tag wins, else n_prompt/n_gen.
function _benchRowOffset(row) {
  const byName = _BENCH_SERIES_NAMES.indexOf(row.series);
  if (byName !== -1) return byName;
  return _benchSeriesOffset(row.n_prompt ?? 0, row.n_gen ?? 0);
}

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

// Last status text written while the stream was healthy, restored after a
// transient drop (the SG guard counts drops and re-opens a closed source).
let _benchLiveStatus = '';
let _benchReconnecting = false;
const _BENCH_MAX_DROPS = 20;

// Write the benchmark status pill and remember it as the live label.
function _benchStatus(text) {
  const el = document.getElementById('benchStatus');
  if (el) el.textContent = text;
  _benchLiveStatus = text;
}

// Drive the benchmark status pill's chip color (idle=muted, running=warn,
// ok=green, err=red). Mutually exclusive — clears the others.
function _benchSetState(state) {
  const el = document.getElementById('benchStatus');
  if (!el) return;
  el.classList.remove('running', 'ok', 'err');
  if (state === 'running' || state === 'ok' || state === 'err') el.classList.add(state);
}

function _rechartBench() {
  _benchAxisTouched = true;   // user picked an axis — stop overriding with the default
  _benchReplotAll();
}

// Relabels the axes and re-plots every stored row against the current axis selections.
function _benchReplotAll() {
  if (!_benchChart) return;
  const xAxis = document.getElementById('benchXAxis')?.value || 'seq';
  // The bar chart's x scale is always a category — changing axes only re-plots
  // the data and relabels the axes. Rebuilding the chart here left it blank
  // (the recreated instance inherited the old datasets' stale internal state).
  const xs = _benchChart.options?.scales?.x;
  if (xs && xs.title) { xs.title.display = !!xAxis && xAxis !== 'seq'; xs.title.text = xAxis || 'sequence'; }
  const yAxis = document.getElementById('benchYAxis')?.value || 'avg_ts';
  const ys = _benchChart.options?.scales?.y;
  if (ys && ys.title) { ys.title.text = yAxis === 'ms_tok' ? 'ms/tok' : yAxis === 'avg_ts' ? 't/s' : yAxis; }
  _benchChart.data.datasets.forEach(d => { d.data = []; });
  _benchRawRows.forEach(r => {
    const dsIdx = _benchModelDatasets[r.model_id];
    if (dsIdx === undefined) return;
    const x = String(_benchGetX(r));   // bar chart needs category (string) x values
    const y = _benchGetY(r);
    const off = _benchRowOffset(r);
    if (off !== null) _benchChart.data.datasets[dsIdx + off].data.push({x, y});
  });
  _benchSyncChartLabels();
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
  kv:          'KV cache type (K/V)',
  type_k:      'K cache type',
  type_v:      'V cache type',
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
  load_mode:   'Load mode (--load-mode)',
  avg_ts:      'Avg tokens/sec',
  stddev_ts:   'Std-dev tokens/sec',
  // Additional JSONL summary fields
  pp:          'Prompt tokens per seq (pp)',
  tg:          'Gen tokens per seq (tg)',
  pl:          'Parallel sequences (pl)',
  n_kv_max:    'Max KV cache (n_kv_max)',
  // Custom-switch shortcuts the user types in the switches panel
  t:           'CPU threads (-t)',
  ngl:         'GPU layers (-ngl)',
  fa:          'Flash-attention (-fa)',
  ctk:         'KV cache type — K (-ctk)',
  ctv:         'KV cache type — V (-ctv)',
  ncmoe:       'Non-cache MoE experts (-ncmoe)',
  mmp:         'No-mmap (-mmp)',
  lm:          'Load mode (-lm)',
  c:           'Context size (-c)',
  p:           'Prompt batch (p)',
  n:           'Gen tokens (n)',
  d:           'Depth (d)',
  b:           'Batch (b)',
  ub:          'Micro-batch (ub)',
  npp:         'Prompt tokens per seq (-npp)',
  ntg:         'Gen tokens per seq (-ntg)',
  npl:         'Parallel sequences (-npl)',
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
  load_mode:    'lm',
  type_k:       'ctk',
  type_v:       'ctv',
  kv:           'kv',
};

// Dynamically populate axis selects from numeric keys found in raw rows
// AND from the user's custom switch flags so a sweep over e.g. --threads
// can be plotted even before any results have arrived for the current run.
// Inline mirror of js/lib/benchaxis.js's computeBenchAxisOptions — used only
// when that script failed to load, so axis dropdowns still populate. benchaxis.js
// stays the canonical unit-tested source when present.
function _benchAxisOptsFallback(rows, switches, labelFn) {
  const SKIP = new Set(['ts', 'seq', 'gen_tps', 'ppt_tps', 'pg_tps', 'model_id', 'avg_ts', 'ms_tok']);
  const label = typeof labelFn === 'function' ? labelFn : (k) => k;
  rows = Array.isArray(rows) ? rows : [];
  const STR_KEYS = new Set(['type_k', 'type_v', 'kv']);
  const distinct = {};
  rows.forEach((r) => {
    Object.entries(r || {}).forEach(([k, v]) => {
      if (SKIP.has(k)) return;
      if (typeof v !== 'number' && !(typeof v === 'string' && STR_KEYS.has(k))) return;
      (distinct[k] = distinct[k] || new Set()).add(v);
    });
  });
  const varied = Object.keys(distinct).filter((k) => distinct[k].size >= 2);
  const FLAG_TO_FIELD = {
    p: 'n_prompt', n: 'n_gen', d: 'n_depth', b: 'n_batch', ub: 'n_ubatch',
    t: 'n_threads', ngl: 'n_gpu_layers', fa: 'flash_attn', ctk: 'type_k', ctv: 'type_v', mmp: 'no_mmap', lm: 'load_mode',
    npp: 'pp', ntg: 'tg', npl: 'pl',
  };
  const switchKeys = [];
  (switches || []).forEach((sw) => {
    if (!sw || typeof sw.flag !== 'string') return;
    const name = sw.flag.replace(/^--?/, '').trim();
    if (name) switchKeys.push(FLAG_TO_FIELD[name] || name);
  });
  const fieldKeys = [...new Set([...varied, ...switchKeys])].sort();
  const xOptions = [...fieldKeys, 'seq'].map((k) => ({ v: k, t: label(k) }));
  const yOptions = [
    { v: 'avg_ts', t: 'Avg tokens/sec' },
    { v: 'ms_tok', t: 'Milliseconds per token' },
    ...fieldKeys.filter((k) => k !== 'avg_ts').map((k) => ({ v: k, t: label(k) })),
  ];
  const defaultX = fieldKeys.includes('kv') ? 'kv' : (fieldKeys.includes('n_depth') ? 'n_depth' : (fieldKeys[0] || 'seq'));
  return { xOptions, yOptions, defaultX, defaultY: 'avg_ts' };
}

function _updateBenchAxisOpts() {
  const xSel = document.getElementById('benchXAxis');
  const ySel = document.getElementById('benchYAxis');
  if (!xSel || !ySel) return;
  const curX = xSel.value;
  const curY = ySel.value;

  // Use the canonical (unit-tested) benchaxis.js when it loaded; otherwise the
  // inline fallback, so the dropdowns populate even if that script is missing.
  const computeFn = (typeof computeBenchAxisOptions === 'function') ? computeBenchAxisOptions : _benchAxisOptsFallback;
  const { xOptions, yOptions, defaultX, defaultY } =
    computeFn(_benchRawRows, _benchSwitches, _benchAxisLabel);

  const fill = (sel, opts, cur, dflt) => {
    sel.innerHTML = '';
    opts.forEach(({ v, t }) => {
      const opt = document.createElement('option');
      opt.value = v;
      opt.textContent = t;
      sel.appendChild(opt);
    });
    sel.value = (_benchAxisTouched && opts.some(o => o.v === cur)) ? cur : dflt;
  };
  fill(xSel, xOptions, curX, defaultX);
  fill(ySel, yOptions, curY, defaultY);
}

function _benchAddModelDatasets(modelId) {
  // Lazy-create the chart if openBench's init didn't stick, so results always plot.
  if (!_benchChart) { try { _benchChart = _mkBenchChart('benchChart'); } catch (e) { console.warn('benchChart init failed', e); } }
  if (!_benchChart) return;
  if (_benchModelDatasets[modelId] !== undefined) return;
  const colorIdx = Object.keys(_benchModelDatasets).length % BENCH_SERIES_COLORS.length;
  const colors = BENCH_SERIES_COLORS[colorIdx];
  const shortName = modelId.split('/').pop() || modelId;
  _benchModelDatasets[modelId] = _benchChart.data.datasets.length;
  _benchChart.data.datasets.push(..._BENCH_SERIES_NAMES.map(s => (
    { label: shortName + ' ' + s, data: [], borderColor: colors[s], backgroundColor: colors[s] + '40',
      borderWidth: 1, pointRadius: 3, pointHoverRadius: 7, fill: false }
  )));
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
    // Backend-loader chatter adds no benchmark signal — keep it out of the log.
    if (/^(ggml_[\w.]*:|load_backend:)/.test(t)) return '';
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
    const typeLabel = _BENCH_SERIES_NAMES[_benchSeriesOffset(nP, nG) ?? 2];
    const typeCls = typeLabel;
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
      '-lm': 'load_mode', '--load-mode': 'load_mode',
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
    const fields = _benchLogFieldSpans(baseFields);
    const yType = document.getElementById('benchYAxis')?.value;
    const dispVal = yType === 'ms_tok' ? (ts > 0 ? (1000/ts).toFixed(2).padStart(8) + ' ms/tok' : '—')
                                       : ts.toFixed(2).padStart(8) + ' t/s';
    return `<div class="bench-log-result">
      <span class="bench-log-type ${typeCls}">${typeLabel}</span>
      <span class="bench-log-fields">${fields}</span>
      <span class="bench-log-tps">${dispVal}${sd}</span>
    </div>`;
  }

  // Batched-bench result row — one line per config with pp/tg/combined speeds
  if (obj.pp !== undefined && obj.tg !== undefined && obj.speed !== undefined) {
    const fields = _benchLogFieldSpans([
      ['pp', obj.pp], ['tg', obj.tg], ['pl', obj.pl ?? 0],
      ['b', obj.n_batch ?? 0], ['ub', obj.n_ubatch ?? 0],
    ]);
    const parts = `pp ${Number(obj.speed_pp ?? 0).toFixed(1)} · tg ${Number(obj.speed_tg ?? 0).toFixed(1)}`;
    return `<div class="bench-log-result">
      <span class="bench-log-type pg">pg</span>
      <span class="bench-log-fields">${fields}</span>
      <span class="bench-log-tps">${Number(obj.speed).toFixed(2).padStart(8)} t/s <span style="color:var(--fg-faint)">(${parts})</span></span>
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

// Per-key value slot widths (chars) so monospace log rows form exact columns.
const _BENCH_FIELD_PAD = { p:7, n:7, d:7, b:6, ub:6, ngl:4, fa:3, pg:10, ctk:6, ctv:6, t:4, pp:6, tg:6, pl:4 };

// Renders [key, value] pairs as the log line's field spans, space-padded to
// each key's slot width so columns align across rows.
function _benchLogFieldSpans(pairs) {
  return pairs.map(([k, v]) => {
    const txt = String(v);
    const slot = _BENCH_FIELD_PAD[k] ?? (txt.length + 2);
    const pad = ' '.repeat(Math.max(2, slot - txt.length + 2));
    return `<span class="bench-log-field"><span>${_hEsc(String(k))}:</span><b>${_hEsc(txt)}${pad}</b></span>`;
  }).join('');
}

function _hEsc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _benchPushPoint(msg) {
  // Build raw row — capture all numeric fields from JSONL result for dynamic axis options
  const raw = { model_id: msg.model_id, ts: Date.now(), seq: _benchRawRows.length,
                gen_tps: msg.gen_tps, ppt_tps: msg.ppt_tps, pg_tps: msg.pg_tps,
                series: msg.series };
  Object.entries(msg).forEach(([k, v]) => {
    if (typeof v === 'number' && k !== 'gen_tps' && k !== 'ppt_tps' && k !== 'pg_tps') raw[k] = v;
  });
  if (typeof msg.type_k === 'string' && msg.type_k) raw.type_k = msg.type_k;
  if (typeof msg.type_v === 'string' && msg.type_v) raw.type_v = msg.type_v;
  if (raw.type_k && raw.type_v) raw.kv = raw.type_k + '/' + raw.type_v;
  _benchRawRows.push(raw);
  // Axis-option update is a side-effect — never let it abort chart plotting below.
  const xBefore = document.getElementById('benchXAxis')?.value;
  try { _updateBenchAxisOpts(); } catch (e) { console.warn('bench axis-opts update failed', e); }

  if (!_benchChart) return;
  // A flipped default X axis re-plots every stored row so earlier points keep the same category scale.
  if (xBefore !== undefined && document.getElementById('benchXAxis')?.value !== xBefore) { _benchReplotAll(); return; }
  const dsIdx = _benchModelDatasets[msg.model_id];
  if (dsIdx === undefined) return;
  let x = _benchGetX(raw);
  const y = _benchGetY(raw);
  x = String(x);
  const off = _benchRowOffset(msg);
  if (off !== null) _benchChart.data.datasets[dsIdx + off].data.push({x, y});
  _benchSyncChartLabels();
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
};

// Load stored benchmark results for all models on startup, to show badges on model cards and have data ready on bench open
async function loadBenchmarkData() {
  try {
    const d = await fetch('/api/benchmark/results').then(r => r.json());
    _benchData = {};
    (d.results || []).forEach(r => { _benchData[r.model_id] = r; });
  } catch (e) {
    // Keep the previously loaded data — blanking it would wipe every badge.
    console.warn('loadBenchmarkData failed:', e);
  }
}

// Idle-entry init for the Benchmark module; shown by toolsOpenTool first.
// Re-entry after a finished run keeps its results/chart/log — only the model
// list is rebuilt (picks preserved); runBenchmark does its own reset.
let _benchOpenedOnce = false;
async function openBench(modelId) {
  const fresh = !_benchOpenedOnce;
  _benchOpenedOnce = true;

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
  const prevChecked = new Set(
    [...panel.querySelectorAll('input[type=checkbox]:checked')].map(c => c.value));
  panel.innerHTML = '';
  models.forEach(m => {
    const item = document.createElement('div');
    item.className = 'bench-model-item';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = m;
    cb.id = 'benchcb_' + m.replace(/[^a-zA-Z0-9]/g, '_');
    if (m === modelId || prevChecked.has(m)) cb.checked = true;
    cb.addEventListener('change', _updateBenchModelLabel);
    const lbl = document.createElement('label');
    lbl.htmlFor = cb.id;
    lbl.textContent = m;
    item.appendChild(cb);
    item.appendChild(lbl);
    panel.appendChild(item);
  });
  _updateBenchModelLabel();

  if (!fresh) return;

  // First open: reset UI state — clear data BEFORE switchBenchTab so its axis
  // update sees a clean slate and honors the default axes (n_depth / avg_ts).
  _benchModelDatasets = {};
  _benchRawRows = [];
  _benchAxisTouched = false;
  if (_benchChart) {
    _benchChart.data.datasets = [];
    _benchChart.data.labels = [];
    _benchChart.update('none');
  }
  switchBenchTab('llama-bench');
  _benchLogClear();
  _benchRenderPlaceholder();
  document.getElementById('benchStatus').textContent = 'idle';
  _benchSetState('idle');
  document.getElementById('benchRunBtn').disabled = false;
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

// Run id from an SSE event's `<run>:<seq>` id, for ledger de-duplication.
function _runIdOf(ev) {
  return String((ev && ev.lastEventId) || '').split(':')[0] || '';
}

// Records a finished run into the cross-tool ledger (#770); fire-and-forget.
function _recordToolRun(tool, data) {
  fetch('/api/tools/runs', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tool, ...data}),
  }).catch(() => {});
}

// Best t/s per test type for a model from the raw rows of the current run.
function _benchMaxes(modelId) {
  const modelRows = _benchRawRows.filter(r => r.model_id === modelId);
  const maxOf = fn => {
    const vals = modelRows.filter(fn).map(r => r.avg_ts ?? 0);
    return vals.length ? Math.max(...vals) : null;
  };
  return { ppt: maxOf(r => _benchRowOffset(r) === 0),
           gen: maxOf(r => _benchRowOffset(r) === 1),
           pg:  maxOf(r => _benchRowOffset(r) === 2) };
}

// Switch between different benchmark tools (llama-bench, etc.) and load their default switches into the UI
function switchBenchTab(tool) {
  document.querySelectorAll('.bench-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tool);
  });
  _benchSwitches = (BENCH_DEFAULTS[tool] || []).map(s => ({...s}));
  _renderBenchSwitches();
  _updateBenchSwitchLabel();
  _updateBenchAxisOpts();   // populate axis dropdowns from the default switches on open
}

// Known switches per tool for the structured editor; type drives the control.
const _BENCH_KV_QUANTS = ['f16', 'bf16', 'q8_0', 'q5_1', 'q5_0', 'q4_1', 'q4_0'];
const BENCH_SWITCH_DEFS = {
  'llama-bench': [
    {flag:'-ngl', label:'-ngl (gpu layers)', type:'number'},
    {flag:'-fa',  label:'-fa (flash attn)',  type:'select', options:['0','1']},
    {flag:'-pg',  label:'-pg (prompt,gen)',  type:'text'},
    {flag:'-p',   label:'-p (prompt sizes)', type:'text'},
    {flag:'-n',   label:'-n (gen sizes)',    type:'text'},
    {flag:'-d',   label:'-d (depths)',       type:'text'},
    {flag:'-b',   label:'-b (batch)',        type:'text'},
    {flag:'-ub',  label:'-ub (ubatch)',      type:'text'},
    {flag:'-ctk', label:'-ctk (K cache types)', type:'multi', options:_BENCH_KV_QUANTS},
    {flag:'-ctv', label:'-ctv (V cache types)', type:'multi', options:_BENCH_KV_QUANTS},
    {flag:'-t',   label:'-t (threads)',      type:'text'},
  ],
};

function _benchActiveTool() {
  return 'llama-bench';
}

function _benchDefaultFor(tool, flag) {
  return (BENCH_DEFAULTS[tool] || []).find(s => s.flag === flag)?.value ?? '';
}

// Warns when -ctv selects a quantized V cache without -fa 1.
function _benchKvHint() {
  const hint = document.querySelector('#benchSwitchList .bench-sw-hint');
  if (!hint) return;
  const ctv = _benchSwitches.find(s => s.flag === '-ctv');
  const fa = _benchSwitches.find(s => s.flag === '-fa');
  const vals = String(ctv?.value ?? '').split(',').map(s => s.trim()).filter(Boolean);
  const quantized = vals.some(v => v !== 'f16' && v !== 'bf16');
  const faOk = !!fa && String(fa.value).trim() === '1';
  hint.textContent = (quantized && !faOk) ? 'quantized V cache needs -fa 1' : '';
}

// Render the switches editor: one typed row per known switch (checkbox +
// label + input/select), then a free-form custom section at the bottom.
function _renderBenchSwitches() {
  const list = document.getElementById('benchSwitchList');
  if (!list) return;
  list.innerHTML = '';
  const tool = _benchActiveTool();
  const defs = BENCH_SWITCH_DEFS[tool] || [];
  const knownFlags = new Set(defs.map(d => d.flag));

  const refresh = () => { _updateBenchSwitchLabel(); _updateBenchAxisOpts(); _benchKvHint(); };

  defs.forEach(def => {
    const entry = _benchSwitches.find(s => s.flag === def.flag);
    const row = document.createElement('div');
    row.className = 'bench-opt-row';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !!entry;

    const lbl = document.createElement('label');
    lbl.className = 'bench-opt-label';
    lbl.textContent = def.label;
    lbl.addEventListener('click', () => cb.click());

    let input;
    if (def.type === 'multi') {
      input = document.createElement('span');
      input.className = 'bench-sw-chips';
      const cur = String(entry?.value ?? _benchDefaultFor(tool, def.flag) ?? '');
      const on = new Set(cur.split(',').map(s => s.trim()).filter(Boolean));
      if (!on.size) on.add(def.options[0]);
      const csv = () => def.options.filter(o => on.has(o)).join(',');
      input.value = csv();
      def.options.forEach(opt => {
        const chip = document.createElement('span');
        chip.className = 'bench-chip' + (on.has(opt) ? ' on' : '');
        chip.dataset.opt = opt; chip.textContent = opt;
        chip.addEventListener('click', (e) => {
          e.stopPropagation();
          if (input.disabled) return;
          if (on.has(opt)) { if (on.size === 1) return; on.delete(opt); } else on.add(opt);
          chip.classList.toggle('on', on.has(opt));
          input.value = csv();
          input.dispatchEvent(new Event('change'));
        });
        input.appendChild(chip);
      });
    } else if (def.type === 'select') {
      input = document.createElement('select');
      const opts = [...def.options];
      const cur = entry?.value ?? _benchDefaultFor(tool, def.flag);
      if (cur !== '' && !opts.includes(cur)) opts.unshift(cur);
      opts.forEach(v => {
        const o = document.createElement('option');
        o.value = v; o.textContent = v;
        input.appendChild(o);
      });
      input.value = cur;
    } else {
      input = document.createElement('input');
      input.type = def.type === 'number' ? 'number' : 'text';
      input.value = entry?.value ?? _benchDefaultFor(tool, def.flag);
    }
    if (def.type !== 'multi') input.className = 'bench-input';
    input.disabled = !entry;
    input.classList.toggle('off', !!input.disabled);
    input.addEventListener('change', () => {
      const sw = _benchSwitches.find(s => s.flag === def.flag);
      if (sw) { sw.value = input.value; refresh(); }
    });

    cb.addEventListener('change', () => {
      if (cb.checked) {
        _benchSwitches.push({flag: def.flag, value: input.value || _benchDefaultFor(tool, def.flag)});
      } else {
        _benchSwitches = _benchSwitches.filter(s => s.flag !== def.flag);
      }
      input.disabled = !cb.checked;
      input.classList.toggle('off', !!input.disabled);
      refresh();
    });

    row.appendChild(cb);
    row.appendChild(lbl);
    row.appendChild(input);
    if (def.flag === '-ctv') {
      const hint = document.createElement('div');
      hint.className = 'bench-sw-hint';
      row.appendChild(hint);
    }
    list.appendChild(row);
  });
  _benchKvHint();

  // Custom section: free-form rows for anything outside the known set.
  const head = document.createElement('div');
  head.className = 'bench-opt-custom-h';
  head.textContent = 'Custom';
  list.appendChild(head);

  _benchSwitches.forEach((sw, i) => {
    if (knownFlags.has(sw.flag)) return;
    const row = document.createElement('div');
    row.className = 'bench-switch-row';

    const flagInput = document.createElement('input');
    flagInput.className = 'bench-input';
    flagInput.placeholder = '-flag';
    flagInput.value = sw.flag || '';
    flagInput.addEventListener('change', () => {
      _benchSwitches[i].flag = flagInput.value;
      _updateBenchAxisOpts();   // surface the new flag in the axis dropdown
    });

    const valInput = document.createElement('input');
    valInput.className = 'bench-input';
    valInput.placeholder = 'value';
    valInput.value = sw.value || '';
    valInput.addEventListener('change', () => { _benchSwitches[i].value = valInput.value; });

    const delBtn = document.createElement('button');
    delBtn.className = 'bench-del-btn';
    delBtn.textContent = '✕';
    delBtn.addEventListener('click', (e) => {
      // Stop the click reaching boot.js's document handler: the re-render below
      // detaches this button, so closest('.bench-dropdown') would read null and
      // close the switch dropdown.
      e.stopPropagation();
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

// Add a new empty custom switch and focus its flag input
function addBenchSwitch() {
  _benchSwitches.push({flag:'', value:''});
  _renderBenchSwitches();
  _updateBenchSwitchLabel();
  _updateBenchAxisOpts();
  document.getElementById('benchSwitchPanel').classList.add('open');
  const rows = document.querySelectorAll('#benchSwitchList .bench-switch-row .bench-input');
  if (rows.length) rows[rows.length - 2].focus();
}

// Host CPU mode note in the run header; blank when the agent's perf controller is off.
function _benchPerfNote(ev) {
  const el = document.getElementById('benchPerf'); if (!el) return;
  el.textContent = typeof perfModeNote === 'function' ? perfModeNote(ev) : '';
}

// Starts the benchmark: gathers selected models, tool and switches, starts the run on the
// agent, and streams results into the UI. The agent owns the host perf mode for the run.
async function runBenchmark() {
  const modelIds = [...document.querySelectorAll('#benchModelPanel input[type=checkbox]:checked')]
                     .map(cb => cb.value);
  const tool     = 'llama-bench';
  const switches = _benchSwitches.filter(s => (s.flag || '').trim());
  if (!modelIds.length) { alert('Select at least one model.'); return; }

  // Guard re-entry and disable the run button.
  const runBtn = document.getElementById('benchRunBtn');
  if (runBtn.disabled) return;
  runBtn.disabled = true;

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
      if (!ok) { runBtn.disabled = false; return; }
      if (loadedModel) {
        document.getElementById('benchStatus').textContent = 'unloading model…';
        _benchSetState('running');
        try {
          await fetch('/api/llm/unload', {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({model: loadedModel.id})
          });
        } catch(_) {}
      }
      if (serverUp) {
        document.getElementById('benchStatus').textContent = 'stopping server…';
        _benchSetState('running');
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

  _benchPerfNote(null);
  document.getElementById('benchStatus').textContent = 'starting…';
  _benchSetState('running');
  document.getElementById('benchResults').classList.remove('shown');
  document.getElementById('benchResultRows').innerHTML = '';
  document.getElementById('benchCancelBtn').style.display = '';
  _benchLogClear();
  if (_benchChart) {
    _benchChart.data.datasets = [];
    _benchChart.data.labels = [];
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
      _benchSetState('idle');
      return;
    }
    if (_benchEventSrc) { try { _benchEventSrc.close(); } catch(_){} }
    _benchStatus('running…');
    _benchEventSrc = SG.open({
      url: '/api/benchmark/stream', maxDrops: _BENCH_MAX_DROPS,
      onReconnecting: () => {
        _benchReconnecting = true;
        document.getElementById('benchStatus').textContent = 'reconnecting…';
      },
      onRestored: () => { _benchReconnecting = false; _benchStatus(_benchLiveStatus); },
      onLost: () => {
        _benchReconnecting = false;
        _benchEventSrc = null; if (typeof toolsSyncRunDot === 'function') toolsSyncRunDot();
        document.getElementById('benchRunBtn').disabled = false;
        document.getElementById('benchCancelBtn').style.display = 'none';
        document.getElementById('benchStatus').textContent = 'disconnected';
        _benchSetState('err');
        _benchPerfNote(null);
      },
      onEvent: (msg, e) => {
      if (msg.type === 'model_start') {
        _benchAddModelDatasets(msg.model_id);
        _benchLogAppend(`<div class="bench-log-sep">── ${_hEsc(msg.model_id)} ──</div>`);
        if (msg.cmd) _benchLogAppend(`<span class="bench-log-cmd">$ ${_hEsc(msg.cmd)}</span>`);
        _benchStatus(`running: ${msg.model_id.split('/').pop()}`);
      } else if (msg.type === 'line') {
        const html = _benchFormatLine(msg.text || '');
        if (html) _benchLogAppend(html);
      } else if (msg.type === 'result') {
        if (msg.model_id && (msg.gen_tps != null || msg.ppt_tps != null || msg.pg_tps != null)) {
          _benchPushPoint(msg);
        }
      } else if (msg.type === 'model_done') {
        // The agent sends its own maxes from v2026.08.31-1 on; matching them
        // keeps this row identical to the one the agent records (#772).
        const mx = msg.max_gen_tps === undefined
          ? _benchMaxes(msg.model_id)
          : { gen: msg.max_gen_tps, ppt: msg.max_ppt_tps, pg: msg.max_pg_tps };
        const energy = { wh_per_ktok: msg.wh_per_ktok ?? null, energy_wh: msg.energy_wh ?? null,
                          energy_source: msg.energy_source || null };
        _benchAddModelResultRow(msg.model_id, tool, mx, energy);
        document.getElementById('benchResults').classList.add('shown');
        _recordToolRun('benchmark', {model_id: msg.model_id, gen_tps: mx.gen,
                                     ppt_tps: mx.ppt, pg_tps: mx.pg, bench_tool: tool,
                                     run_id: msg.run_id || _runIdOf(e),
                                     wh_per_ktok: energy.wh_per_ktok,
                                     ok: mx.gen != null || mx.ppt != null || mx.pg != null});
        if (energy.wh_per_ktok != null) {
          _benchLogAppend(`<span class="bench-log-text">energy ${(energy.energy_wh ?? 0).toFixed(2)} Wh · ${energy.wh_per_ktok.toFixed(2)} Wh / 1k tokens (${_hEsc(energy.energy_source || '—')})</span>`);
        } else if ('wh_per_ktok' in msg) {
          _benchLogAppend(`<span class="bench-log-text">energy: no power reading</span>`);
        }
      } else if (msg.type === 'perf_mode') {
        _benchPerfNote(msg);
      } else if (msg.type === 'done') {
        if (_benchEventSrc) { try { _benchEventSrc.close(); } catch(_){} _benchEventSrc = null; } if (typeof toolsSyncRunDot === 'function') toolsSyncRunDot();
        document.getElementById('benchRunBtn').disabled = false;
        document.getElementById('benchCancelBtn').style.display = 'none';
        _benchStatus(msg.ok ? 'done' : (msg.error ? 'error' : 'done'));
        _benchSetState(msg.ok ? 'ok' : 'err');
        if (msg.error) _benchLogAppend(`<span class="bench-log-text" style="color:var(--crit)">✗ Error: ${_hEsc(String(msg.error))}</span>`);
        _benchPerfNote(null);
      }
      },
    });
    if (typeof toolsSyncRunDot === "function") toolsSyncRunDot();
  }).catch(e => {
    alert('Benchmark request failed: ' + e);
    document.getElementById('benchRunBtn').disabled = false;
    document.getElementById('benchCancelBtn').style.display = 'none';
    document.getElementById('benchStatus').textContent = 'idle';
    _benchSetState('idle');
  });
}

// Function to cancel a running benchmark: closes the event stream, sends a cancel request to the backend, and updates the UI state
function cancelBenchmark() {
  _benchReconnecting = false;
  if (_benchEventSrc) { try { _benchEventSrc.close(); } catch(_){} _benchEventSrc = null; } if (typeof toolsSyncRunDot === 'function') toolsSyncRunDot();
  fetch('/api/benchmark/cancel', {method: 'POST'}).catch(() => {});
  document.getElementById('benchRunBtn').disabled = false;
  document.getElementById('benchCancelBtn').style.display = 'none';
  document.getElementById('benchStatus').textContent = 'cancelled';
  _benchSetState('idle');
  _benchPerfNote(null);
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
  ['Prompt', 'Generation', 'Combined', 'Energy'].forEach(label => {
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
function _benchAddModelResultRow(modelId, tool, maxes, energy) {
  const { ppt: maxPpt, gen: maxGen, pg: maxPg } = maxes || _benchMaxes(modelId);
  const en = energy || {};

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
    saveBenchmark(modelId, maxGen, maxPpt, maxPg, tool, saveBtn, en.wh_per_ktok != null ? en : null);
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
  const eTile = mkStat('Energy', en.wh_per_ktok, 'Wh/1k tok', 2);
  if (en.wh_per_ktok == null) eTile.title = 'no power reading';
  else if (en.energy_source) { const s = document.createElement('span'); s.className = 'bench-stat-src'; s.textContent = en.energy_source; eTile.querySelector('.bench-stat-k').appendChild(s); }
  grid.appendChild(eTile);

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
function saveBenchmark(model_id, avg_gen_tps, avg_ppt_tps, avg_pg_tps, tool, saveBtn, extra) {
  if (!model_id) return;
  const body = {model_id, avg_gen_tps, avg_ppt_tps, avg_pg_tps, bench_tool: tool, switches: _benchSwitches};
  if (extra && typeof extra === 'object') body.extra_json = extra;
  fetch('/api/benchmark/store', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(body)
  }).then(r => r.json()).then(d => {
    if (!d.ok) { alert(d.error || 'Save failed'); return; }
    _benchData[model_id] = {model_id, avg_gen_tps, avg_ppt_tps, avg_pg_tps, bench_tool: tool,
                            switches: _benchSwitches, ts: new Date().toISOString(), extra_json: extra || null};
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
