// Run-conditions block (#1302): flattened rows, two-run comparison, details table.
// IIFE-scoped, exposed as window.COND.
(function (root, factory) {
  const api = factory();
  if (typeof root !== 'undefined') root.COND = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function () {

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

const _has = v => v != null && v !== '';
const _str = v => (_has(v) ? String(v) : null);
const _gib = n => (Number(n) / 2 ** 30).toFixed(1);

function fmtBytes(n) {
  return _has(n) && isFinite(n) ? _gib(n) + ' GiB' : null;
}

const _o = (c, k) => (c && c[k] && typeof c[k] === 'object' ? c[k] : {});
const _snap = (c, side) => _o(_o(c, 'snapshot'), side);
const _late = (c, k) => (_has(_snap(c, 'end')[k]) ? _snap(c, 'end')[k] : _snap(c, 'start')[k]);

// Start→end row spec: snapshot key, unit, compare threshold, value formatter.
function _pair(key, label, sk, unit, thr, fmt) {
  const f = fmt || String;
  return { key, label, pair: { sk, thr },
    get: c => {
      const s = _snap(c, 'start')[sk], e = _snap(c, 'end')[sk];
      if (!_has(s) && !_has(e)) return null;
      return `${_has(s) ? f(s) : '—'} → ${_has(e) ? f(e) : '—'} ${unit}`;
    } };
}

const ROWS = [
  { key: 'gpu', label: 'GPU', get: c => _str(_o(c, 'hardware').gpu) },
  { key: 'driver', label: 'Driver', get: c => _str(_o(c, 'hardware').driver) },
  { key: 'cpu', label: 'CPU', get: c => _str(_o(c, 'hardware').cpu) },
  { key: 'cores', label: 'Cores', get: c => {
    const h = _o(c, 'hardware');
    const p = [];
    if (_has(h.cpu_cores)) p.push(`${h.cpu_cores} cores`);
    if (_has(h.cpu_threads)) p.push(`${h.cpu_threads} threads`);
    return p.length ? p.join(' · ') : null;
  } },
  { key: 'ram', label: 'RAM', get: c => fmtBytes(_o(c, 'hardware').ram_bytes) },
  { key: 'os', label: 'OS', get: c => _str(_o(c, 'os').name) },
  { key: 'kernel', label: 'Kernel', get: c => _str(_o(c, 'os').kernel) },
  { key: 'backend', label: 'Backend', get: c => _str(_o(c, 'backend').name) },
  { key: 'build', label: 'Build', get: c => _str(_o(c, 'backend').build) },
  { key: 'server_cmd', label: 'Server command', get: c => _str(_o(c, 'backend').server_cmd),
    norm: v => v.replace(/\s+/g, ' ').trim() },
  { key: 'model_file', label: 'Model file', get: c => _str(_o(c, 'model').file) },
  { key: 'file_id', label: 'File id', get: c => _str(_o(c, 'model').hash) },
  { key: 'size', label: 'Size', get: c => fmtBytes(_o(c, 'model').size_bytes) },
  { key: 'quantization', label: 'Quantization', get: c => _str(_o(c, 'model').quantization) },
  { key: 'context', label: 'Context', get: c => (_has(c && c.context) && isFinite(c.context)
    ? String(Math.round(Number(c.context))) : null) },
  { key: 'temperature', label: 'Temperature', get: c => _str(_o(c, 'sampling').temperature) },
  { key: 'top_k', label: 'Top-k', get: c => _str(_o(c, 'sampling').top_k) },
  { key: 'top_p', label: 'Top-p', get: c => _str(_o(c, 'sampling').top_p) },
  { key: 'min_p', label: 'Min-p', get: c => _str(_o(c, 'sampling').min_p) },
  { key: 'repeat_penalty', label: 'Repeat penalty', get: c => _str(_o(c, 'sampling').repeat_penalty) },
  { key: 'seed', label: 'Seed', get: c => _str(_o(c, 'sampling').seed) },
  _pair('gpu_temp', 'GPU temp start→end', 'temp_c', '°C', 5),
  _pair('clock', 'Clock start→end', 'sclk_mhz', 'MHz', 100),
  _pair('power', 'Power start→end', 'power_w', 'W', 10),
  { key: 'power_cap', label: 'Power cap', get: c => (_has(_late(c, 'power_cap_w')) ? `${_late(c, 'power_cap_w')} W` : null) },
  { key: 'performance_level', label: 'Performance level', get: c => _str(_late(c, 'performance_level')) },
  { key: 'governor', label: 'Governor', get: c => _str(_late(c, 'cpu_governor')) },
  _pair('ram_free', 'RAM free start→end', 'ram_available_bytes', 'GiB', 2 * 2 ** 30, _gib),
];

function rows(cond) {
  if (!cond || typeof cond !== 'object') return [];
  return ROWS.map(r => ({ key: r.key, label: r.label, value: r.get(cond) })).filter(r => r.value != null);
}

// One start→end pair differs: either side missing on one run only, or the gap exceeds the threshold.
function _pairChanged(a, b, sk, thr) {
  return ['start', 'end'].some(side => {
    const va = _snap(a, side)[sk], vb = _snap(b, side)[sk];
    if (!_has(va) && !_has(vb)) return false;
    if (!_has(va) || !_has(vb)) return true;
    return Math.abs(Number(va) - Number(vb)) > thr;
  });
}

function diff(a, b) {
  const ca = a && typeof a === 'object' ? a : null, cb = b && typeof b === 'object' ? b : null;
  return ROWS.map(r => {
    const va = ca ? r.get(ca) : null, vb = cb ? r.get(cb) : null;
    if (va == null && vb == null) return null;
    let changed;
    if (va == null || vb == null) changed = true;
    else if (r.pair) changed = _pairChanged(ca, cb, r.pair.sk, r.pair.thr);
    else if (r.norm) changed = r.norm(va) !== r.norm(vb);
    else changed = va !== vb;
    return { key: r.key, label: r.label, a: va, b: vb, changed };
  }).filter(Boolean);
}

function html(cond, opts) {
  const rs = rows(cond);
  if (!rs.length) return '';
  const open = opts && opts.open === true ? ' open' : '';
  const body = rs.map(r => `<tr><th>${esc(r.label)}</th><td${r.key === 'server_cmd' ? ' class="wrap"' : ''}>` +
    `${esc(r.value)}</td></tr>`).join('');
  return `<details class="cond-block"${open}><summary>Conditions</summary>` +
    `<table class="cond-t"><tbody>${body}</tbody></table></details>`;
}

return { rows, diff, html, fmtBytes };
});
