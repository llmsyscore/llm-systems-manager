// Host page view-model transforms (#1432): one page per host for llama.cpp, LM Studio, vLLM or no LLM server.
// Pure functions only — no fetch, no DOM. Exposed as window.HP; reads OV builders and HV bucketing helpers.
(function (root, factory) {
  const ov = (root && root.OV) || (typeof require === 'function' ? require('./overall-view') : null);
  const hv = (root && root.HV) || (typeof require === 'function' ? require('./hosts-view') : null);
  const pk = (root && root.LMPeaks) || (typeof require === 'function' ? require('./peaks') : null);
  const api = factory(ov, hv, pk);
  if (typeof root !== 'undefined') root.HP = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis, function (OV, HV, LMPeaks) {

const DASH = '—';
const PAGES = {
  llama: { provider: 'llama', label: 'llama.cpp', sub: 'llamacpp' },
  lms: { provider: 'lms', label: 'LM Studio', sub: 'lmstudio' },
  vllm: { provider: 'vllm', label: 'vLLM', sub: 'vllm' },
  host: { provider: null, label: 'Host', sub: 'host' },
  mgr: { provider: null, label: 'Manager host', sub: 'manager' },
};
// Manager page panels outside the System row, kept by mapLegacyLayout.
const MGR_PANELS = ['services', 'db', 'streams', 'hosts', 'procs', 'tiles', 'perf', 'ae-perf', 'influx-perf', 'tower'];
const ROWS = [['tiles', 'Host'], ['inf', 'Inference'], ['gpu', 'GPU'], ['sys', 'System'], ['hw', 'Power hardware']];
const _P = (row, name, title, kind) => ({ row, name, title, kind });
const PANELS = [
  _P('tiles', 'tiles', 'Snapshot', 'tiles'),
  _P('inf', 'tps', 'Throughput', 'ts'),
  _P('inf', 'server', 'Server', 'kv'),
  _P('inf', 'details', 'Server details', 'kv'),
  _P('inf', 'gen', 'Tokens per hour', 'ts'),
  _P('inf', 'models', 'Models', 'list'),
  _P('inf', 'requests', 'Requests', 'kv'),
  _P('inf', 'kv', 'KV cache', 'ts'),
  _P('gpu', 'gpu-busy', 'GPU busy and memory', 'ts'),
  _P('gpu', 'gpu-temp', 'GPU temperature and power', 'ts'),
  _P('gpu', 'gpu-details', 'GPU details', 'kv'),
  _P('gpu', 'mac-power', 'Mac power', 'split'),
  _P('gpu', 'mac-gpu', 'GPU busy', 'ts'),
  _P('sys', 'cpu-mem', 'CPU and memory', 'ts'),
  _P('sys', 'cpu-details', 'CPU details', 'kv'),
  _P('sys', 'mem-details', 'Memory details', 'kv'),
  _P('sys', 'net', 'Network', 'ts'),
  _P('sys', 'io', 'Disk activity', 'ts'),
  _P('sys', 'disk', 'Disk space', 'bars'),
  _P('hw', 'ups', 'UPS', 'kv'),
  _P('hw', 'aio', 'Liquid cooler', 'kv'),
  _P('hw', 'psu', 'Power supply', 'kv'),
  _P('hw', 'fans', 'Fan controller', 'table'),
];
const ONLY_ON = { models: 'lms', requests: 'vllm', kv: 'vllm' };
// Alarm metrics [source, name] whose threshold lines a chart draws.
const CHART_METRIC = {
  'cpu-mem': [['system', 'cpu_total'], ['system', 'ram_percent']],
  'gpu-busy': [['system', 'gpu_gpu_util_percent'], ['system', 'gpu_vram_usage_percent']],
  'disk': [['system', 'disk_root_percent']], 'mac-gpu': [['mac_power', 'gpu_busy_pct']],
};
const LEGACY_IDS = {
  'llama-server': 'llama-server', 'llama-throughput': 'llama-tps', 'gpu': 'llama-gpu-busy',
  'cpu-overall': 'llama-cpu-mem', 'ram': 'llama-mem-details', 'network': 'llama-net', 'disk-usage': 'llama-disk',
  'disk-io': 'llama-io', 'ups': 'llama-ups', 'aio': 'llama-aio', 'psu': 'llama-psu', 'smart-device': 'llama-fans',
  'lms-models': 'lms-models', 'lms-active': 'lms-server', 'lms-cpu': 'lms-cpu-mem', 'lms-ram': 'lms-mem-details',
  'lms-network': 'lms-net', 'lms-disk': 'lms-disk', 'lms-io': 'lms-io', 'lms-power': 'lms-mac-power',
  'vllm-server': 'vllm-server', 'vllm-requests': 'vllm-requests', 'vllm-kv': 'vllm-kv', 'vllm-throughput': 'vllm-tps',
  'vllm-cpu': 'vllm-cpu-mem', 'vllm-ram': 'vllm-mem-details', 'vllm-network': 'vllm-net', 'vllm-disk': 'vllm-disk',
  'vllm-io': 'vllm-io',
};
const OLD_GRID_KEYS = ['order', 'hidden', 'cols', 'lmsOrder', 'lmsHidden', 'lmsCols', 'vllmOrder', 'vllmHidden', 'vllmCols'];
// Layout key of each page's hidden panel ids, and the old grid hidden list each one maps from.
const HIDDEN_KEYS = { llama: 'llamaPanelsHidden', lms: 'lmsPanelsHidden', vllm: 'vllmPanelsHidden', host: 'hostPanelsHidden' };
const OLD_HIDDEN = { llama: 'hidden', lms: 'lmsHidden', vllm: 'vllmHidden' };
// Old card ids a hidden list does not carry over to the new panels.
const HIDDEN_NOT_MAPPED = ['lms-power'];
const CHAT_TITLE_MAX = 600;
// macOS system mounts left out of the Disk space bars; the first also marks a sample as a Mac's.
const MAC_SYS_MOUNTS = ['/System/Volumes/', '/private/var/run/com.apple.security.cryptexd/', '/Library/Developer/CoreSimulator/'];
const LINUX_SYS_MOUNTS = ['/sys', '/proc', '/dev', '/run'];
// Removable drives under /run that stay listed.
const LINUX_KEEP_MOUNTS = ['/run/media'];
const MAC_DATA = '/System/Volumes/Data', MAC_DISK_LABEL = 'Macintosh HD (/)';
const THERMAL = ['Nominal', 'Fair', 'Serious', 'Critical'];
const WINDOW_STATE = { unsupported: 'Not reported', off: 'Not in use', idle: 'Idle', reset: 'Reset', paused: 'Paused' };
const SEV_TONE = { critical: 'bad', warning: 'warn' };

function _num(v) { return (typeof v === 'number' && Number.isFinite(v)) ? v : null; }
function _obj(v) { return v && typeof v === 'object' && !Array.isArray(v) ? v : {}; }
function _arr(v) { return Array.isArray(v) ? v : []; }
function _str(v) { return typeof v === 'string' && v ? v : null; }
function _has(o) { return Object.keys(_obj(o)).length > 0; }
function _now(ms) { return (typeof ms === 'number' && Number.isFinite(ms)) ? ms : Date.now(); }
function _ms(v) {
  if (typeof v === 'number' && Number.isFinite(v)) return v > 1e12 ? v : v * 1000;
  const t = typeof v === 'string' ? Date.parse(v) : NaN;
  return Number.isFinite(t) ? t : null;
}
function _cap(s) { s = String(s || '').toLowerCase(); return s ? s[0].toUpperCase() + s.slice(1) : ''; }
function _short(h) { return String(h || '').replace(/\.local$/, ''); }
function _tail(id) { return String(id || '').split('/').pop(); }
function _fix(v, dp) { return _num(v) == null ? DASH : v.toFixed(dp); }
function _loc(v) { return _num(v) == null ? DASH : v.toLocaleString('en-US'); }
function _txt(v) { return v == null || v === '' ? DASH : String(v); }
// Bytes → "1.5 GB" from 1 GiB up, else whole MB; a dash when unreadable.
function _bytes(v) {
  if (_num(v) == null) return DASH;
  const gb = v / 1073741824;
  return gb >= 1 ? gb.toFixed(1) + ' GB' : (v / 1048576).toFixed(0) + ' MB';
}
// A liquidctl reading's value; null when absent.
function _lq(dev, key) {
  const e = _obj(dev)[key];
  if (!e) return null;
  return e.value != null ? e.value : null;
}
// A liquidctl reading's text: numbers to one decimal, other values as they are, a dash when absent.
function _lqText(v) { return v == null ? DASH : (typeof v === 'number' ? v.toFixed(1) : String(v)); }

// {live, state, models, peaks, now, lastLlama} or a bare live payload → its parts; state and models are llama's llama-state and
// /api/llm/models, peaks the rate trackers {tps, pps} and slot peaks {val, ts}, lastLlama the last llama block with counters {ll, at}.
function _inf(inf) {
  const o = _obj(inf);
  return 'live' in o || 'state' in o
    ? { live: _obj(o.live), state: o.state, models: o.models, peaks: _obj(o.peaks), now: _now(o.now), lastLlama: o.lastLlama || null }
    : { live: o, state: undefined, models: undefined, peaks: {}, now: Date.now(), lastLlama: null };
}

function panelsFor(page) {
  const p = Object.prototype.hasOwnProperty.call(PAGES, page) ? page : 'host';
  if (p === 'mgr') return PANELS.filter(x => x.row === 'sys');
  return PANELS.filter(x => !(p === 'host' && x.row === 'inf') && (!ONLY_ON[x.name] || ONLY_ON[x.name] === p));
}

function _gpu(g) {
  g = _obj(g);
  const out = {
    name: _str(g.name), util: _num(g.gpu_util_percent), vramPct: _num(g.vram_usage_percent),
    vramUsedMb: _num(g.vram_used_mb), gttPct: _num(g.gtt_usage_percent), gttUsed: _num(g.gtt_used_bytes),
    gttTotal: _num(g.gtt_total_bytes), temp: _num(g.temperature_c), junction: _num(g.temperature_junction_c),
    memTemp: _num(g.temperature_memory_c), coreMv: _num(g.vddgfx_mv), offsetMv: _num(g.voltage_offset_mv),
    power: _num(g.power_watts), powerCap: _num(g.power_cap_watts), sclk: _num(g.sclk_mhz), mclk: _num(g.mclk_mhz),
    fanRpm: _num(g.fan1_rpm), perfLevel: _str(g.performance_level), powerProfile: _str(g.power_profile),
  };
  return Object.values(out).some(v => v != null) ? out : null;
}
function _mac(m) {
  if (!_has(m)) return null;
  const tn = _num(m.thermal_pressure_n);
  return {
    socW: _num(m.soc_total_w), cpuW: _num(m.cpu_package_w), gpuW: _num(m.gpu_w), aneW: _num(m.ane_w),
    thermalN: tn, thermal: (tn != null ? THERMAL[Math.round(tn)] : null) || _str(m.thermal_pressure),
    gpuBusy: _num(m.gpu_busy_pct), pcoreMhz: _num(m.pcore_freq_mhz), pcorePct: _num(m.pcore_util_pct),
    ecoreMhz: _num(m.ecore_freq_mhz), ecorePct: _num(m.ecore_util_pct), gpuMhz: _num(m.gpu_freq_mhz),
    netIn: _num(m.net_in_pkts_s), netOut: _num(m.net_out_pkts_s),
  };
}
function _ups(u) {
  u = _obj(u);
  const out = { percent: _num(u.percent), state: _str(u.state), warning: _str(u.warning_level),
    onBattery: typeof u.on_battery === 'boolean' ? u.on_battery : null,
    timeToEmpty: _str(u.time_to_empty), timeToFull: _str(u.time_to_full) };
  return Object.values(out).some(v => v != null) ? out : null;
}
function _aio(k) {
  if (!_has(k)) return null;
  return { name: _str(k._name), temp: _lq(k, 'Liquid temperature'), pumpRpm: _lq(k, 'Pump speed'),
    pumpDuty: _lq(k, 'Pump duty'), fanRpm: _lq(k, 'Fan speed'), fanDuty: _lq(k, 'Fan duty') };
}
function _psu(p) {
  if (!_has(p)) return null;
  return { name: _str(p._name), vrmTemp: _lq(p, 'VRM temperature'), caseTemp: _lq(p, 'Case temperature'),
    fanRpm: _lq(p, 'Fan speed'), inputV: _lq(p, 'Input voltage'), totalOut: _lq(p, 'Total power output'),
    inputPower: _lq(p, 'Estimated input power'), efficiency: _lq(p, 'Estimated efficiency') };
}
function _fans(sd) {
  if (!_has(sd)) return null;
  const out = _arr(sd.fans).filter(f => f && typeof f === 'object').map(f => ({
    id: f.id != null ? String(f.id) : '', mode: _str(f.control_mode), duty: f.duty != null ? f.duty : null,
    speed: f.speed && f.speed.value != null ? f.speed.value : null, unit: f.speed ? _str(f.speed.unit) : null,
    volts: _num(f.voltage_v), milliamps: f.current_ma != null ? f.current_ma : null,
  }));
  out.name = _str(sd._name);
  return out;
}

// Flat llama sample, nested {system, mac_power} sample or the system route's {sample} → one host view;
// opts.page 'lms' or 'vllm' drops mounts of 100 MB or less, as those pages did.
function normalize(raw, opts) {
  const small = ['lms', 'vllm'].includes(_obj(opts).page);
  let r = _obj(raw);
  if (!_has(r.system) && _has(r.sample)) r = _obj(r.sample);
  const s = _has(r.system) ? r.system : r;
  const ram = _obj(s.ram), swap = _obj(s.swap), net = _obj(s.net), io = _obj(s.disk_io), lq = _obj(s.liquidctl);
  const isc = _obj(s.iscsi), target = _str(isc.target);
  const disks = _arr(s.disk).filter(d => d && _str(d.mountpoint) && !(small && _num(d.total_bytes) != null && d.total_bytes <= 1e8))
    .map(d => ({ mount: d.mountpoint, percent: _num(d.percent), used: _num(d.used_bytes), total: _num(d.total_bytes) }));
  const macPower = _mac(r.mac_power || s.mac_power);
  return {
    mac: !!macPower || disks.some(d => d.mount.indexOf(MAC_SYS_MOUNTS[0]) === 0),
    cpu: _num(s.cpu_total), cores: _arr(s.cpu_per_core).map(_num), cpuTemp: _num(s.cpu_temp_c),
    governor: _str(s.cpu_governor), cpuName: _str(s.cpu_name),
    ram: { percent: _num(ram.percent), used: _num(ram.used_bytes), total: _num(ram.total_bytes),
           cached: _num(ram.cached_bytes), buffers: _num(ram.buffers_bytes), available: _num(ram.available_bytes) },
    swap: { used: _num(swap.used_bytes), free: _num(swap.free_bytes), percent: _num(swap.percent) },
    gpu: _gpu(s.gpu), macPower,
    net: { sent: _num(net.bytes_sent_per_sec != null ? net.bytes_sent_per_sec : net.bytes_sent_per_s),
           recv: _num(net.bytes_recv_per_sec != null ? net.bytes_recv_per_sec : net.bytes_recv_per_s) },
    io: { read: _num(io.read_bytes_per_sec), write: _num(io.write_bytes_per_sec) },
    disks,
    iscsi: (_str(isc.state) || target) ? { state: _str(isc.state), target: target ? target.split(':').pop() || target : null } : null,
    ups: _ups(s.ups), aio: _aio(lq.aio), psu: _psu(lq.psu), fans: _fans(lq.smart),
    ts: _ms(r.ts != null ? r.ts : s.ts),
  };
}

// Disks for the Disk space bars, real drives only: Linux leaves out /sys, /proc, /dev and /run mounts; a Mac shows its
// data volume once as "Macintosh HD (/)" plus mounts outside the system ones.
function diskBars(n) {
  const list = _arr(n && n.disks);
  const under = (d, m) => d.mount === m || d.mount.indexOf(m + '/') === 0;
  if (!(n && n.mac)) return list.filter(d => LINUX_KEEP_MOUNTS.some(m => d.mount.indexOf(m + '/') === 0) || !LINUX_SYS_MOUNTS.some(m => under(d, m)));
  const data = list.find(d => d.mount === MAC_DATA) || list.find(d => d.mount === '/');
  const rest = list.filter(d => d.mount !== '/' && d.mount !== MAC_DATA && !MAC_SYS_MOUNTS.some(m => d.mount.indexOf(m) === 0));
  return (data ? [Object.assign({}, data, { label: MAC_DISK_LABEL })] : []).concat(rest);
}

function hasGpu(n) {
  const g = n && n.gpu;
  return !!g && [g.util, g.vramPct, g.vramUsedMb, g.temp, g.power].some(v => v != null);
}

// Generation and prompt tokens/s now for a provider page; nulls when not reported.
function _rates(page, live) {
  if (page === 'llama') {
    const ll = _obj(live.llama);
    return { gen: _num(ll.tokens_per_second), prompt: _num(ll.prompt_tokens_per_second) };
  }
  if (page === 'lms') {
    const gr = _obj(live.gateway_rates);
    return { gen: _num(gr.gen_tps), prompt: _num(gr.prompt_tps) };
  }
  if (page === 'vllm') {
    const v = _obj(live.vllm), up = v.state === 'running';
    return { gen: up ? _num(v.tokens_per_second) : null, prompt: up ? _num(v.prompt_tokens_per_second) : null };
  }
  return { gen: null, prompt: null };
}
const NO_TRACKER = { stats: () => ({ peak: null, avg: null }) };
// LMPeaks.rateStat of a llama or LM Studio rate, from the server window, else the passed tracker; live value only elsewhere.
function _rateStat(page, which, inf) {
  const o = _inf(inf), v = _rates(page, o.live)[which];
  if (page !== 'llama' && page !== 'lms') return { v, mode: null, peak: null, now: o.now };
  const tr = o.peaks[which === 'gen' ? 'tps' : 'pps'];
  const st = LMPeaks.rateStat(tr && typeof tr.stats === 'function' ? tr : NO_TRACKER, v, o.now,
    _obj(o.live.throughput_window)[which]);
  return Object.assign(st, { now: o.now });
}
function _peakText(st) { return st.peak ? 'peak ' + st.peak.v.toFixed(1) + ' · ' + LMPeaks.agoText(st.now - st.peak.t) : ''; }
// "42.8 · live · peak 50.0 · 3m ago": the old rate tile's value, mode and peak; a dash alone with no value.
function _rateText(st) { return st.v == null ? DASH : [_fix(st.v, 1), st.mode, _peakText(st)].filter(Boolean).join(' · '); }
function _loadTone(v) { return v == null ? '' : v >= 90 ? 'bad' : v >= 75 ? 'warn' : 'ok'; }

function tiles(page, n, inf, online) {
  n = n || normalize(null);
  const off = online === false;
  let state = { label: 'Online', tone: 'ok' };
  if (off) state = { label: 'Host offline', tone: 'off' };
  else if (PAGES[page] && PAGES[page].provider) state = serverPanel(page, inf, n, online).state;
  const val = (v, f) => (off ? DASH : f(v));
  const out = [{ label: 'State', value: state.label, tone: state.tone },
    { label: 'CPU', value: val(n.cpu, HV.fmtPct), tone: off ? '' : _loadTone(n.cpu) },
    { label: 'Memory', value: val(n.ram.percent, HV.fmtPct), tone: off ? '' : _loadTone(n.ram.percent) }];
  if (hasGpu(n)) {
    out.push({ label: 'GPU busy', value: val(n.gpu.util, HV.fmtPct), tone: '' });
    out.push({ label: 'VRAM', value: val(n.gpu.vramPct, HV.fmtPct), tone: '' });
  }
  if (page !== 'host') {
    [['prompt', 'Prompt tokens/s'], ['gen', 'Gen tokens/s']].forEach(([which, label]) => {
      const st = _rateStat(page, which, inf);
      out.push({ label, value: val(st.v, v => _fix(v, 1)), tone: '',
                 sub: off || st.v == null ? '' : [st.mode, _peakText(st)].filter(Boolean).join(' · ') });
    });
  }
  return out;
}

// Text for a windowed llama ratio block (percent and state word), plus its counts as "(num of den)" unless both are 0.
function _windowText(b, numKey, denKey) {
  if (!b || typeof b !== 'object') return DASH;
  const pct = _num(b.ratio) != null ? (b.ratio * 100).toFixed(1) + '%' : null;
  const st = WINDOW_STATE[b.state];
  const text = pct == null ? (st || DASH) : (st && b.state !== 'idle' ? `${pct} · ${st}` : pct);
  const noWindow = b.state === 'unsupported' || b.state === 'off', num = b[numKey], den = b[denKey];
  return !noWindow && num != null && den != null && !(Number(num) === 0 && Number(den) === 0)
    ? `${text} (${Number(num).toLocaleString('en-US')} of ${Number(den).toLocaleString('en-US')})` : text;
}
// A count's value: a non-zero count as it is, else "0 (peak N 3m ago)" when a peak is known.
function _withPeak(cur, p, now) {
  if (cur != null && cur !== 0) return _txt(cur);
  const pk = _obj(p);
  if (pk.val == null || pk.val === 0) return cur === 0 ? '0' : DASH;
  const t = _ms(pk.ts);
  return `0 (peak ${pk.val}${t != null ? ' ' + LMPeaks.agoText(now - t) : ''})`;
}
// The chat template cell: its size, a 600-character preview as the title and the full text for the table view.
function _chatCell(ll) {
  const size = _num(ll.chat_template_len) == null ? DASH : (ll.chat_template_len / 1024).toFixed(1) + ' KB';
  const t = _str(ll.chat_template);
  return t ? { text: size, v: size, title: t.slice(0, CHAT_TITLE_MAX), full: size + ' · ' + t } : size;
}
// Counter fields of a llama block; a block with none of them (a sleeping server) shows the last block that had them.
const LLAMA_COUNTERS = ['total_tokens_prompted', 'total_tokens_generated', 'n_decode_total', 'n_tokens_max', 'kv_cache_tokens',
  'kv_cache_usage_ratio', 'n_busy_slots_per_decode', 'total_slots', 'modalities', 'chat_template_len', 'chat_template', 'n_remain',
  'cache_reuse', 'spec_accept'];
function llamaHasCounters(ll) { const b = _obj(ll); return LLAMA_COUNTERS.some(k => b[k] != null); }
// The last-seen llama block {ll, at} the details show, or null while the live block has its own counters.
function _llamaSeen(live, inf) {
  const seen = _inf(inf).lastLlama;
  return !llamaHasCounters(live) && seen && llamaHasCounters(seen.ll) ? seen : null;
}
// "Last seen 14:32 · server sleeping" for llama Server details showing last-seen counters; '' otherwise or offline.
function lastSeenNote(page, inf, online) {
  const o = _inf(inf), seen = page === 'llama' && online !== false ? _llamaSeen(_obj(o.live.llama), inf) : null;
  if (!seen) return '';
  const d = new Date(seen.at), hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  const agg = _obj(o.state).aggregate || _obj(_obj(o.state).residency).aggregate;
  return 'Last seen ' + hm + ' · ' + (agg === 'sleeping' ? 'server sleeping' : 'server not reporting');
}
function _llamaDetails(live, inf) {
  const o = _inf(inf), pk = o.peaks, seen = _llamaSeen(live, inf), ll = seen ? _obj(seen.ll) : live;
  const mods = ll.modalities && typeof ll.modalities === 'object'
    ? (Object.keys(ll.modalities).filter(k => ll.modalities[k]).join(', ') || 'text') : DASH;
  return [
    ['Prompt tokens/s', _rateText(_rateStat('llama', 'prompt', inf))], ['Gen tokens/s', _rateText(_rateStat('llama', 'gen', inf))],
    ['Tokens prompted', _loc(ll.total_tokens_prompted)], ['Tokens generated', _loc(ll.total_tokens_generated)],
    ['Decode calls', _loc(ll.n_decode_total)], ['Peak ctx tokens', _loc(ll.n_tokens_max)],
    ['Requests processing', _withPeak(live.requests_processing, pk.requests_processing, o.now)],
    ['Requests deferred', _withPeak(live.requests_deferred, pk.requests_deferred, o.now)],
    ['Active slots', _withPeak(live.active_slots, pk.active_slots, o.now)], ['Avg busy slots/decode', _fix(ll.n_busy_slots_per_decode, 2)],
    ['KV cache usage %', _num(ll.kv_cache_usage_ratio) == null ? DASH : (ll.kv_cache_usage_ratio * 100).toFixed(1) + '%'],
    ['KV cache tokens', _loc(ll.kv_cache_tokens)],
    ['Prompt cache reuse', _windowText(ll.cache_reuse, 'cached', 'processed')],
    ['Draft tokens accepted', _windowText(ll.spec_accept, 'accepted', 'drafted')],
    ['Tokens remaining', _loc(ll.n_remain)], ['Total slots', _loc(ll.total_slots)], ['Modalities', mods],
    ['Chat template', _chatCell(ll)],
  ];
}
// The LM Studio row the old server card showed: busy first, then a passed active row, then any loaded one.
function _lmsActive(d) {
  const ps = _arr(d.ps).filter(p => p && typeof p === 'object');
  return ps.find(p => p.status && !['IDLE', 'STOPPED'].includes(p.status))
    || (_has(d.active) ? d.active : null) || ps.find(p => p.status && p.status !== 'STOPPED') || null;
}
function _lmsDetails(d, inf) {
  const a = _obj(_lmsActive(d)), gt = _obj(d.gateway_tokens), port = _obj(d.server).port;
  return [
    ['Prompt tokens/s', _rateText(_rateStat('lms', 'prompt', inf))], ['Gen tokens/s', _rateText(_rateStat('lms', 'gen', inf))],
    ['Tokens prompted', gt.prompt != null ? Number(gt.prompt).toLocaleString('en-US') : DASH],
    ['Tokens generated', gt.gen != null ? Number(gt.gen).toLocaleString('en-US') : DASH],
    ['Status', _txt(a.status)], ['Size', _txt(a.size)], ['Context', a.context ? _loc(Number(a.context)) : DASH],
    ['Parallel', _txt(a.parallel)], ['Quant', _txt(a.quant)], ['Port', _txt(port)],
  ];
}
function _vllmDetails(d) {
  const v = _obj(d.vllm), up = d.agent_online !== false && v.state === 'running';
  return [
    ['Model', up ? (v.model || '(no model)') : DASH],
    ['State', d.agent_online === false ? 'Host offline' : (v.state || 'unknown')],
    ['Gen tokens/s', up ? _fix(v.tokens_per_second, 1) : DASH],
    ['Prompt tokens/s', up ? _fix(v.prompt_tokens_per_second, 1) : DASH],
    ['GPU KV cache used', up && _num(v.kv_cache_usage_pct) != null ? v.kv_cache_usage_pct.toFixed(1) + '%' : DASH],
  ];
}
function _mhzPct(mhz, pct) {
  if (mhz == null && pct == null) return DASH;
  return (mhz != null ? Math.round(mhz) + ' MHz' : DASH) + (pct != null ? ' · ' + pct.toFixed(0) + '%' : '');
}

// Key-value rows of a panel; every value is a dash when online is false.
function details(page, panelName, n, inf, online) {
  const rows = _details(page, panelName, n, inf);
  return online === false ? rows.map(r => [r[0], DASH]) : rows;
}
function _details(page, panelName, n, inf) {
  n = n || normalize(null);
  const live = _inf(inf).live;
  if (panelName === 'details') {
    if (page === 'llama') return _llamaDetails(_obj(live.llama), inf);
    if (page === 'lms') return _lmsDetails(live, inf);
    if (page === 'vllm') return _vllmDetails(live);
    return [];
  }
  if (panelName === 'requests') {
    const v = _obj(live.vllm), up = live.agent_online !== false && v.state === 'running';
    return [['Running', up ? _txt(_num(v.requests_running)) : DASH], ['Waiting', up ? _txt(_num(v.requests_waiting)) : DASH]];
  }
  if (panelName === 'gpu-details') {
    const g = n.gpu || {};
    return [
      ['Name', _txt(g.name)], ['°C edge', _fix(g.temp, 1)], ['W draw', _fix(g.power, 1)],
      ['°C junction', _fix(g.junction, 1)], ['°C memory', _fix(g.memTemp, 1)], ['mV core', _txt(g.coreMv)],
      ['mV offset', _txt(g.offsetMv)], ['W cap', _fix(g.powerCap, 0)], ['SCLK MHz', _txt(g.sclk)],
      ['MCLK MHz', _txt(g.mclk)], ['fan RPM', _txt(g.fanRpm)], ['Perf level', _txt(g.perfLevel)],
      ['Power profile', _txt(g.powerProfile)], ['VRAM MB', _loc(g.vramUsedMb)],
      ['GTT MB', g.gttUsed == null ? DASH : Math.round(g.gttUsed / 1048576).toLocaleString('en-US')
        + (g.gttTotal ? ' / ' + Math.round(g.gttTotal / 1048576).toLocaleString('en-US') : '')],
    ];
  }
  if (panelName === 'mac-power') {
    const m = n.macPower || {}, w = v => (v == null ? DASH : v.toFixed(2) + ' W');
    return [
      ['SoC total W', w(m.socW)], ['CPU W', w(m.cpuW)], ['GPU W', w(m.gpuW)], ['ANE W', w(m.aneW)],
      ['Thermal', _txt(m.thermal)], ['GPU busy %', _num(m.gpuBusy) == null ? DASH : m.gpuBusy.toFixed(1) + '%'],
      ['P-core', _mhzPct(m.pcoreMhz, m.pcorePct)], ['E-core', _mhzPct(m.ecoreMhz, m.ecorePct)],
      ['GPU clock', m.gpuMhz == null ? DASH : Math.round(m.gpuMhz) + ' MHz'],
      ['Network pkts/s', m.netIn == null && m.netOut == null ? DASH
        : `${m.netIn != null ? Math.round(m.netIn) : DASH} in / ${m.netOut != null ? Math.round(m.netOut) : DASH} out`],
    ];
  }
  if (panelName === 'cpu-details') {
    return [['Temperature', n.cpuTemp == null ? DASH : n.cpuTemp.toFixed(1) + '°C'],
      ['Governor', _txt(n.governor)], ['CPU name', _txt(n.cpuName)]];
  }
  if (panelName === 'mem-details') {
    // A Mac's rows leave out Cached and Buffers.
    const lx = n.mac ? [] : [['Cached', _bytes(n.ram.cached)], ['Buffers', _bytes(n.ram.buffers)]];
    return [['Used', _bytes(n.ram.used)], ['Available', _bytes(n.ram.available)]].concat(lx,
      [['Swap used', _bytes(n.swap.used)], ['Swap free', _bytes(n.swap.free)]]);
  }
  if (panelName === 'ups') {
    const u = n.ups || {};
    return [['Battery %', u.percent == null ? DASH : u.percent.toFixed(0) + '%'], ['State', _txt(u.state)],
      ['Warning level', _txt(u.warning)], ['On battery', u.onBattery == null ? DASH : u.onBattery ? 'Yes' : 'No'],
      ['Time to empty', _txt(u.timeToEmpty)], u.timeToFull ? ['Time to full', u.timeToFull] : ['Status', 'Charged']];
  }
  if (panelName === 'aio') {
    const k = n.aio || {};
    return [['Name', _txt(k.name)], ['Liquid °C', _lqText(k.temp)], ['Pump RPM', _lqText(k.pumpRpm)], ['Pump duty %', _lqText(k.pumpDuty)],
      ['Fan RPM', _lqText(k.fanRpm)], ['Fan duty %', _lqText(k.fanDuty)]];
  }
  if (panelName === 'psu') {
    const p = n.psu || {};
    return [['Name', _txt(p.name)], ['VRM °C', _lqText(p.vrmTemp)], ['Case °C', _lqText(p.caseTemp)], ['Fan RPM', _lqText(p.fanRpm)],
      ['Input V', _lqText(p.inputV)], ['Total output W', _lqText(p.totalOut)],
      ['Input power W', _lqText(p.inputPower)], ['Efficiency %', _lqText(p.efficiency)]];
  }
  return [];
}

// Live network and disk-activity rates as the old tiles showed them.
function liveNotes(n) {
  const net = _obj(n && n.net), io = _obj(n && n.io);
  return { net: `${HV.fmtRate(net.sent)} out · ${HV.fmtRate(net.recv)} in`,
           io: `${HV.fmtRate(io.read)} read · ${HV.fmtRate(io.write)} write` };
}

// Fan controller table: one row per fan with the old card's six columns, and the controller name.
function fanTable(n) {
  const rows = _arr(n && n.fans).map(f => [
    'Fan ' + f.id, _txt(f.mode), _txt(f.duty), f.speed != null ? f.speed + (f.unit ? ' ' + f.unit : '') : DASH,
    f.volts != null ? f.volts.toFixed(2) + ' V' : DASH, f.milliamps != null ? f.milliamps + ' mA' : DASH]);
  return { cols: ['Fan', 'Mode', 'Duty %', 'RPM', 'Voltage', 'Current'], rows, name: (n && n.fans && n.fans.name) || null };
}

// Series of each chart: [row field, series name, colour slot, shown only when some row has the field].
const CHARTS = {
  kv: [['vllm_kv', 'KV cache %', 1]],
  'gpu-busy': [['gpu_util', 'GPU busy %', 3], ['gpu_vram', 'VRAM %', 1], ['gpu_gtt', 'GTT %', 2, true]],
  'gpu-temp': [['gpu_temp', 'Temperature °C', 1], ['gpu_power', 'Power W', 2]],
  'mac-gpu': [['mac_gpu_busy', 'GPU busy %', 1]],
  'cpu-mem': [['cpu_total', 'CPU %', 1], ['ram_percent', 'Memory %', 2]],
  net: [['net_sent', 'Out', 1], ['net_recv', 'In', 2]],
  io: [['io_read', 'Read', 1], ['io_write', 'Write', 2]],
  disk: [['disk_root_pct', '/', 1], ['disk_iscsi_pct', '/mnt/iscsi', 2, true]],
  aio: [['aio_temp', 'Liquid °C', 1]],
  psu: [['psu_out', 'Output W', 1], ['psu_in', 'Input W', 2]],
};
// Throughput draws Prompt in the top band and Generation below it; the colour slots stay gen 1, prompt 2.
function _chartDefs(page, chart) {
  if (chart === 'tps') {
    return PAGES[page] && PAGES[page].provider
      ? [[page + '_pps', 'Prompt t/s', 2], [page + '_tps', 'Gen t/s', 1]] : [];
  }
  return CHARTS[chart] || [];
}

// tsHtml chart styles: peak bars for the rate charts, a busy strip over memory steps for GPU busy and memory.
const CHART_STYLE = {
  tps: { bars: 'peak', split: true, idle: 'Idle · no requests in this range' },
  'mac-gpu': { bars: 'peak' },
  'gpu-busy': { strip: 'GPU busy %', stripLabel: 'Busy', step: true },
  gen: { mode: 'stack', idleLine: true, barFill: 0.8, idle: 'Idle · no tokens in this range' },
};
// Bar charts use coarser buckets (15 minutes over 24 hours).
const BAR_BUCKET_MS = { 6: 300000, 12: 600000, 24: 900000 };
function chartStyle(chartName) { return Object.assign({}, CHART_STYLE[chartName] || {}); }
// Alarm metric of each chart's strip series, whose threshold lines are left out of the chart.
const STRIP_METRIC = { 'gpu-busy': 'gpu_gpu_util_percent' };
// The alarm metrics whose threshold lines a chart draws.
function chartMetrics(chartName) { return _arr(CHART_METRIC[chartName]).filter(m => m[1] !== STRIP_METRIC[chartName]); }

// Stored alarm engine metric [source, name, link label when not the series name] of each chart row field.
const STORED_METRIC = {
  cpu_total: ['system', 'cpu_total'], ram_percent: ['system', 'ram_percent'],
  gpu_util: ['system', 'gpu_gpu_util_percent'], gpu_vram: ['system', 'gpu_vram_usage_percent'], gpu_gtt: ['system', 'gpu_gtt_usage_percent'],
  gpu_temp: ['system', 'gpu_temperature_c'], gpu_power: ['system', 'gpu_power_watts'], mac_gpu_busy: ['mac_power', 'gpu_busy_pct'],
  net_sent: ['system', 'net_bytes_sent_per_sec', 'network out'], net_recv: ['system', 'net_bytes_recv_per_sec', 'network in'],
  io_read: ['system', 'disk_io_read_bytes_per_sec', 'disk read'], io_write: ['system', 'disk_io_write_bytes_per_sec', 'disk write'],
  disk_root_pct: ['system', 'disk_root_percent', 'disk / used %'],
  llama_tps: ['llama', 'tokens_per_second'], llama_pps: ['llama', 'prompt_tokens_per_second'],
  lms_tps: ['gateway', 'lms_tokens_per_second'], lms_pps: ['gateway', 'lms_prompt_tokens_per_second'],
};
// Stored token counter metrics [source, name, label] of each provider page's Tokens chart.
const TOKEN_METRICS = {
  llama: [['llama', 'total_tokens_generated', 'tokens generated'], ['llama', 'total_tokens_prompted', 'tokens prompted']],
  lms: [['gateway', 'lms_gen_tokens_total', 'gateway tokens generated'], ['gateway', 'lms_prompt_tokens_total', 'gateway tokens prompted']],
  vllm: [['vllm', 'total_tokens_generated', 'tokens generated'], ['vllm', 'total_tokens_prompted', 'tokens prompted']],
};
// "View <series> in metrics" links of a chart's shown series that have a stored metric; the Tokens chart links its page's counters.
function metricLinks(page, chartName, rows) {
  if (chartName === 'gen') return _arr(TOKEN_METRICS[page]).map(m => ({ label: 'View ' + m[2] + ' in metrics', source: m[0], name: m[1] }));
  const list = _arr(rows);
  return _chartDefs(page, chartName).filter(d => !d[3] || list.some(r => r && _num(r[d[0]]) != null))
    .filter(d => STORED_METRIC[d[0]])
    .map(d => { const m = STORED_METRIC[d[0]]; return { label: 'View ' + (m[2] || d[1]) + ' in metrics', source: m[0], name: m[1] }; });
}

// Generated and prompt tokens per hour over the range ending at nowMs, from /api/energy/hourly rows; an hour with no row is null.
function genSeries(rows, rangeH, nowMs) {
  const hours = [6, 12, 24].includes(Number(rangeH)) ? Number(rangeH) : 24, b = 3600000;
  const end = Math.floor(_now(nowMs) / b) * b, start = end - (hours - 1) * b;
  const gen = new Array(hours).fill(null), prompt = new Array(hours).fill(null);
  _arr(rows).forEach(r => {
    const t = _num(r && r.hour_ts), i = t == null ? -1 : Math.floor((t * 1000 - start) / b);
    if (!(i >= 0 && i < hours)) return;
    gen[i] = (gen[i] || 0) + (_num(r.tokens_gen) || 0);
    prompt[i] = (prompt[i] || 0) + (_num(r.tokens_prompt) || 0);
  });
  return { labels: OV.bucketLabels(start, hours, b),
           series: [{ name: 'Generated', values: gen, slot: 1 }, { name: 'Prompt', values: prompt, slot: 2 }] };
}

// Bucket means of each chart field over the range ending at nowMs (gaps of up to 3 buckets hold); peak-bar charts take
// each bucket's max with its mean as avg and no held gaps.
function series(page, chartName, rows, rangeH, nowMs) {
  const bars = (CHART_STYLE[chartName] || {}).bars === 'peak';
  const hours = [6, 12, 24].includes(Number(rangeH)) ? Number(rangeH) : 24, b = bars ? BAR_BUCKET_MS[hours] : OV.rangeBucketMs(hours);
  const n = Math.round(hours * 3600000 / b);
  const end = Math.floor(_now(nowMs) / b) * b, start = end - (n - 1) * b;
  const labels = OV.bucketLabels(start, n, b), list = _arr(rows);
  const defs = _chartDefs(page, chartName).filter(d => !d[3] || list.some(r => r && _num(r[d[0]]) != null));
  const out = defs.map(([key, name, slot]) => {
    const sum = new Array(n).fill(0), cnt = new Array(n).fill(0);
    const max = new Array(n).fill(null);
    for (const r of list) {
      const t = _ms(r && r.ts), v = _num(r && r[key]);
      if (t == null || v == null) continue;
      const i = Math.floor((t - start) / b);
      if (i < 0 || i >= n) continue;
      sum[i] += v; cnt[i] += 1;
      if (max[i] == null || v > max[i]) max[i] = v;
    }
    const mean = sum.map((s, i) => (cnt[i] ? s / cnt[i] : null));
    if (bars) return { name, values: max, avg: mean, slot };
    return { name, values: HV.holdShortGaps(mean), slot };
  });
  return { labels, series: RATE_CHARTS.includes(chartName) ? _rateUnit(out) : out };
}
const RATE_CHARTS = ['net', 'io'], RATE_UNITS = ['B/s', 'KB/s', 'MB/s', 'GB/s'];
// Byte-rate series in one 1024-based unit picked from their largest value, named with it ("Out (KB/s)").
function _rateUnit(list) {
  let max = 0, i = 0;
  list.forEach(s => s.values.forEach(v => { if (v != null && Math.abs(v) > max) max = Math.abs(v); }));
  while (max >= 1024 && i < RATE_UNITS.length - 1) { max /= 1024; i++; }
  const f = Math.pow(1024, i);
  return list.map(s => Object.assign({}, s, { name: s.name + ' (' + RATE_UNITS[i] + ')',
    values: s.values.map(v => (v == null ? null : v / f)) }));
}

// One /api/history-shaped row from the live sample, stamped at tsMs (else the sample time, else now).
function liveRow(page, n, inf, tsMs) {
  n = n || normalize(null);
  const live = _inf(inf).live, g = n.gpu || {}, m = n.macPower || {};
  const disk = mount => { const d = n.disks.find(x => x.mount === mount); return d ? d.percent : null; };
  const t = _num(tsMs) != null ? tsMs : (n.ts != null ? n.ts : Date.now());
  const row = {
    ts: new Date(t).toISOString(), cpu_total: n.cpu, ram_percent: n.ram.percent,
    gpu_util: _num(g.util), gpu_vram: _num(g.vramPct), gpu_gtt: _num(g.gttPct), gpu_temp: _num(g.temp),
    gpu_power: _num(g.power), net_sent: n.net.sent, net_recv: n.net.recv, io_read: n.io.read, io_write: n.io.write,
    ups_percent: n.ups ? n.ups.percent : null, disk_root_pct: disk('/'), disk_iscsi_pct: disk('/mnt/iscsi'),
    aio_temp: _num(n.aio && n.aio.temp), psu_out: _num(n.psu && n.psu.totalOut), psu_in: _num(n.psu && n.psu.inputPower),
    mac_gpu_busy: _num(m.gpuBusy),
  };
  const rt = _rates(page, live);
  if (page === 'llama') {
    const ll = _obj(live.llama);
    Object.assign(row, { llama_tps: rt.gen, llama_pps: rt.prompt, llama_ctx: _num(ll.n_tokens_max),
      llama_gen_tokens: _num(ll.total_tokens_generated) });
  } else if (page === 'lms') {
    Object.assign(row, { lms_tps: rt.gen, lms_pps: rt.prompt });
  } else if (page === 'vllm') {
    const v = _obj(live.vllm), up = v.state === 'running';
    Object.assign(row, { vllm_tps: rt.gen, vllm_pps: rt.prompt, vllm_kv: up ? _num(v.kv_cache_usage_pct) : null,
      vllm_req_running: up ? _num(v.requests_running) : null, vllm_req_waiting: up ? _num(v.requests_waiting) : null });
  }
  return row;
}

// One-host aggregate for OV's llama builder: loaded models from residency (else /api/llm/models), awake unless sleeping or off.
function _llamaAgg(st, models, online) {
  const s = _obj(st), res = _obj(s.residency);
  let loaded = _arr(res.models).filter(x => x && (!x.provider || x.provider === 'llama') && x.status === 'loaded').length;
  if (!_arr(res.models).length) loaded = _arr(_obj(models).data).filter(x => x && _obj(x.status).value === 'loaded').length;
  const agg = s.aggregate || res.aggregate;
  const awake = online !== false && s.agent_online !== false && ['active', 'idle', 'loading'].includes(agg);
  return { agent_count_total: 1, agent_count_online: online === false ? 0 : 1, awake_agent_count: awake ? 1 : 0,
           active_model_count: loaded };
}
// The old LM Studio model list: installed models (embeddings left out) with their ps status, then ps-only rows.
function _lmsModels(d) {
  const norm = id => String(id || '').replace(/[/-]/g, '_').toLowerCase();
  const ps = _arr(d.ps).filter(p => p && typeof p === 'object');
  const shown = _arr(d.models).filter(m => m && m.id && !String(m.id).toLowerCase().includes('embed'));
  const rows = shown.map(m => {
    const pr = ps.find(p => norm(p.identifier || p.model) === norm(m.id));
    return { name: m.id, status: (pr && pr.status) || 'IDLE', size: (pr && pr.size) || '' };
  }).concat(ps.filter(p => {
    const pn = norm(p.identifier || p.model);
    return !shown.some(m => norm(m.id) === pn) && !String(p.identifier || p.model || '').toLowerCase().includes('embed');
  }).map(p => ({ name: p.model || p.identifier, status: p.status || 'IDLE', size: p.size || '' })));
  return rows.map(r => {
    const st = String(r.status || 'IDLE').toUpperCase(), on = !['IDLE', 'STOPPED', ''].includes(st);
    return { label: String(r.name || ''), title: String(r.name || ''), status: _cap(st), tone: on ? 'ok' : 'off', sub: r.size };
  });
}
function _vllmPanel(d, online) {
  const v = _obj(d.vllm), off = online === false || d.agent_online === false, up = !off && v.state === 'running';
  const state = off ? { label: 'Host offline', tone: 'off', detail: '' }
    : up ? { label: 'Server on', tone: 'ok', detail: v.port ? `port ${v.port}` : '' }
    : { label: 'Server off', tone: 'off', detail: '' };
  // The server's own state word is in Server details; here the State pill alone shows it.
  const kv = [['Model', up && v.model ? _tail(v.model) : 'none served']];
  if (up) {
    kv.push(['Requests', `${_num(v.requests_running) || 0} running · ${_num(v.requests_waiting) || 0} waiting`]);
    if (_num(v.kv_cache_usage_pct) != null) kv.push(['KV cache', `${v.kv_cache_usage_pct.toFixed(0)}% full`]);
    kv.push(['Speed', `${_fix(_num(v.tokens_per_second) || 0, 1)} t/s · prompt ${_fix(_num(v.prompt_tokens_per_second) || 0, 1)} t/s`]);
  }
  return { state, note: '', kv, pills: [], models: [], power: null,
           hint: up || off ? null : 'Start the server from LLM Control to see its requests and cache here.',
           table: { cols: ['Field', 'Value'], rows: [['State', state.label]].concat(kv) } };
}

// Server panel for one host in OV's shape; null on the host page.
function serverPanel(page, inf, n, online) {
  const { live, state, models } = _inf(inf);
  let p = null;
  if (page === 'llama') {
    const st = Object.assign({}, _obj(state), online === false ? { agent_online: false } : {});
    p = OV.llamaPanel(st, models, _llamaAgg(st, models, online), live.host || null);
    p.table.rows = p.table.rows.filter(r => r[0] !== 'Hosts awake');
    p.note = _short(st.host || live.host);
  } else if (page === 'lms') {
    const d = Object.assign({}, live, online === false ? { agent_online: false } : {});
    p = OV.lmsPanel(d, { agent_count_total: 1, agent_count_online: online === false ? 0 : 1,
      loaded_model_count_total: _arr(d.ps).length });
    p.models = _lmsModels(d);
    p.note = _short(_obj(d.system).host);
  } else if (page === 'vllm') {
    p = _vllmPanel(live, online);
    p.note = _short(_obj(live.system).host || live.host);
  }
  if (p && !('power' in p)) p.power = null;
  return p;
}

// Threshold lines of enabled rules for source/metric on this host, as thresholds.js matches them;
// unlabelled, the severity shows by colour only.
function thresholdsFor(rules, source, metric, host) {
  const h = host != null ? host : null, out = [];
  _arr(rules).forEach(rule => {
    if (!rule || !rule.enabled || rule.metric_source !== source || rule.metric_name !== metric) return;
    if (rule.source_host && rule.source_host !== h) return;
    const t = _obj(_obj(rule.config).threshold), vals = [];
    if (rule.rule_type === 'threshold_above') { const v = t.upper ?? t.value ?? t.critical ?? t.warning; if (v != null) vals.push(v); }
    else if (rule.rule_type === 'threshold_below') { const v = t.lower ?? t.value ?? t.warning ?? t.critical; if (v != null) vals.push(v); }
    else if (rule.rule_type === 'threshold_range') { if (t.lower != null && t.upper != null) vals.push(t.lower, t.upper); }
    vals.forEach(value => out.push({ value, label: '', tone: SEV_TONE[rule.severity] || 'muted' }));
  });
  return out;
}

const PANEL_IDS = new Set();
Object.keys(PAGES).forEach(pg => panelsFor(pg).forEach(p => PANEL_IDS.add(pg + '-' + p.name)));
MGR_PANELS.forEach(name => PANEL_IDS.add('mgr-' + name));
// Old card id → new panel id; null drops it. New panel ids and ids in keep stay; other ids drop.
function _mapId(id, keep) {
  if (Object.prototype.hasOwnProperty.call(LEGACY_IDS, id)) return LEGACY_IDS[id];
  return PANEL_IDS.has(id) || keep.includes(id) ? id : null;
}
function _dedupe(list) { return list.filter((x, i) => x != null && list.indexOf(x) === i); }

// Old hidden lists of a provider grid and its per-host lists (union) → that page's hidden panel ids.
function _mapHidden(layout, map) {
  const byAgent = _obj(layout.hiddenByAgent);
  Object.keys(OLD_HIDDEN).forEach(page => {
    const old = _arr(layout[OLD_HIDDEN[page]]).slice();
    Object.values(_obj(byAgent[page])).forEach(list => { old.push(..._arr(list)); });
    const ids = old.map(id => (typeof id === 'string' && !HIDDEN_NOT_MAPPED.includes(id) ? map(id) : null))
      .filter(id => id != null && id.indexOf(page + '-') === 0 && PANEL_IDS.has(id));
    if (!ids.length) return;
    const key = HIDDEN_KEYS[page], cur = _arr(layout[key]);
    layout[key] = _dedupe(cur.concat(ids));
  });
}

// Old provider-grid ids in pins, Overall order, sizes and hidden lists → new panel ids, once; old grid keys deleted.
// keepIds lists other pinnable card ids (the manager cards) that stay as they are.
function mapLegacyLayout(layout, keepIds) {
  if (!layout || typeof layout !== 'object' || Array.isArray(layout)) return { changed: false };
  if (layout.hostPageMapped) {
    if (layout.lmsMacPowerShown) return { changed: false };
    // Once: a layout mapped earlier shows the LM Studio Mac power panel again.
    if (Array.isArray(layout.lmsPanelsHidden)) layout.lmsPanelsHidden = layout.lmsPanelsHidden.filter(id => id !== 'lms-mac-power');
    layout.lmsMacPowerShown = 1;
    return { changed: true };
  }
  const keep = _arr(keepIds), map = id => _mapId(id, keep);
  if (Array.isArray(layout.overallBorrowed)) layout.overallBorrowed = _dedupe(layout.overallBorrowed.map(map));
  if (Array.isArray(layout.overallOrder)) {
    layout.overallOrder = _dedupe(layout.overallOrder.map(id => {
      if (typeof id !== 'string' || id.indexOf('ov-borrow-') !== 0) return id;
      const to = map(id.slice(10));
      return to == null ? null : 'ov-borrow-' + to;
    }));
  }
  const cs = layout.cardSizes;
  if (cs && typeof cs === 'object' && !Array.isArray(cs)) {
    Object.keys(cs).forEach(k => {
      const pre = k.indexOf('ov-borrow-') === 0 ? 'ov-borrow-' : '', id = k.slice(pre.length);
      if (!Object.prototype.hasOwnProperty.call(LEGACY_IDS, id) || LEGACY_IDS[id] === id) return;
      const to = LEGACY_IDS[id], v = cs[k];
      delete cs[k];
      if (to != null && !(pre + to in cs)) cs[pre + to] = v;
    });
  }
  _mapHidden(layout, map);
  OLD_GRID_KEYS.forEach(k => { delete layout[k]; });
  ['hiddenByAgent', 'orderByAgent', 'sizesByAgent'].forEach(k => {
    const b = layout[k];
    if (b && typeof b === 'object') ['llama', 'lms', 'vllm'].forEach(p => { delete b[p]; });
    if (!b || typeof b !== 'object' || !Object.keys(b).length) delete layout[k];
  });
  delete layout.agentGrids;
  layout.hostPageMapped = 1;
  layout.lmsMacPowerShown = 1;
  return { changed: true };
}

return { PAGES, ROWS, PANELS, CHART_METRIC, LEGACY_IDS, HIDDEN_KEYS, panelsFor, normalize, hasGpu, diskBars, genSeries, tiles, details, liveNotes, fanTable,
         series, chartStyle, chartMetrics, metricLinks, liveRow, serverPanel, thresholdsFor, mapLegacyLayout, llamaHasCounters, lastSeenNote };
});
