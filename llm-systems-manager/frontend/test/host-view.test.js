// #1432 phase 4c-2: host page transforms in js/lib/host-view.js.
// Live samples: flat llama /api/metrics, nested /api/lmstudio/metrics and /api/vllm/metrics, system-only route.
import { describe, it, expect } from 'vitest';
import HP from '../js/lib/host-view.js';
import HV from '../js/lib/hosts-view.js';
import OV from '../js/lib/overall-view.js';

const NOW = Date.parse('2026-11-16T12:00:30+00:00');
const MIN = 60000;
const iso = (msAgo) => new Date(NOW - msAgo).toISOString();

const SYS = {
  cpu_total: 23.5, cpu_per_core: [10, 37], cpu_temp_c: 51.2, cpu_governor: 'performance', cpu_name: 'Ryzen 9',
  ram: { total_bytes: 64e9, used_bytes: 20e9, available_bytes: 44e9, percent: 31.2, cached_bytes: 8e9, buffers_bytes: 1e8 },
  swap: { total_bytes: 8e9, used_bytes: 1e9, free_bytes: 7e9, percent: 12.5 },
  net: { bytes_sent_per_sec: 2048, bytes_recv_per_sec: 4096 },
  disk_io: { read_bytes_per_sec: 1000, write_bytes_per_sec: 3000 },
  disk: [{ mountpoint: '/', total_bytes: 5e11, used_bytes: 2e11, percent: 40 },
         { mountpoint: '/mnt/iscsi', total_bytes: 2e12, used_bytes: 1e12, percent: 50 }],
};
const GPU = { name: 'RX 7900', gpu_util_percent: 88, vram_usage_percent: 71.5, vram_used_mb: 17000,
  gtt_usage_percent: 3, gtt_used_bytes: 104857600, gtt_total_bytes: 1073741824,
  temperature_c: 60, temperature_junction_c: 72.4, temperature_memory_c: 66, vddgfx_mv: 950,
  power_watts: 250, power_cap_watts: 300, voltage_offset_mv: -50, sclk_mhz: 2400, mclk_mhz: 1250,
  fan1_rpm: 1400, performance_level: 'auto', power_profile: 'COMPUTE' };
const LQ = {
  aio: { _name: 'Kraken X63', 'Liquid temperature': { value: 31.25, unit: '°C' }, 'Pump speed': { value: 2100, unit: 'rpm' },
         'Pump duty': { value: 60, unit: '%' }, 'Fan speed': { value: 900, unit: 'rpm' }, 'Fan duty': { value: 40, unit: '%' } },
  psu: { _name: 'HX1000i', 'VRM temperature': { value: 45.5 }, 'Case temperature': { value: 38 },
         'Fan speed': { value: 0 }, 'Input voltage': { value: 230 }, 'Total power output': { value: 410.4 },
         'Estimated input power': { value: 455 }, 'Estimated efficiency': { value: 'n/a' } },
  smart: { _name: 'Smart Device', fans: [{ id: 1, control_mode: 'PWM', duty: 40, speed: { value: 800, unit: 'rpm' }, voltage_v: 12.1, current_ma: 90 }] },
};
const LLAMA_FLAT = Object.assign({ ts: iso(0) }, SYS, {
  gpu: GPU, liquidctl: LQ, iscsi: { state: 'LOGGED_IN', target: 'iqn.2004-04.com:nas:lun1' },
  ups: { percent: 98, state: 'charging', warning_level: 'none', on_battery: false, time_to_empty: '1h', time_to_full: null },
  llama: { model: 'qwen', tokens_per_second: 42.75, prompt_tokens_per_second: 300, total_tokens_generated: 12000,
           kv_cache_usage_ratio: 0.25, active_slots: 1, total_slots: 4, modalities: { vision: true, audio: false } },
});
const LMS_MAC = { ts: iso(0), agent_online: true, server: { on: true, port: 1234 },
  ps: [{ identifier: 'llama-3', model: 'llama-3', status: 'GENERATING', size: '4 GB', context: 8192, quant: 'Q4' }],
  models: [{ id: 'llama-3' }, { id: 'text-embed' }, { id: 'mistral' }],
  gateway_rates: { gen_tps: 12.5, prompt_tps: 80 }, gateway_tokens: { gen: 5000, prompt: 9000 },
  system: Object.assign({ host: 'mac.local' }, SYS),
  mac_power: { soc_total_w: 30, cpu_package_w: 10, gpu_w: 18, ane_w: 2, thermal_pressure_n: 1, gpu_busy_pct: 64,
               pcore_freq_mhz: 3200, pcore_util_pct: 40, ecore_freq_mhz: 2000, ecore_util_pct: 10, gpu_freq_mhz: 1200,
               net_in_pkts_s: 50, net_out_pkts_s: 20 } };
const VLLM = { ts: iso(0), agent_online: true,
  vllm: { state: 'running', model: 'org/mistral-7b', port: 8000, requests_running: 2, requests_waiting: 1,
          kv_cache_usage_pct: 33.3, tokens_per_second: 55.5, prompt_tokens_per_second: 120 },
  system: Object.assign({}, SYS, { gpu: GPU }) };
const HOST_ONLY = { agent_id: 'h1', hostname: 'box', online: true, age_s: 2, sample: { ts: iso(0), system: SYS } };

describe('PAGES, ROWS, PANELS and panelsFor', () => {
  it('lists the four pages and five rows', () => {
    expect(HP.PAGES.host).toEqual({ provider: null, label: 'Host', sub: 'host' });
    expect(HP.PAGES.lms).toEqual({ provider: 'lms', label: 'LM Studio', sub: 'lmstudio' });
    expect(HP.ROWS.map(r => r[0])).toEqual(['tiles', 'inf', 'gpu', 'sys', 'hw']);
  });
  it('drops the inference row on the host page and provider-only panels elsewhere', () => {
    const host = HP.panelsFor('host');
    expect(host.some(p => p.row === 'inf')).toBe(false);
    expect(host.map(p => p.name)).toContain('cpu-mem');
    const lms = HP.panelsFor('lms').map(p => p.name);
    expect(lms).toContain('models');
    expect(lms).toContain('gen');
    expect(lms).not.toContain('kv');
    const llama = HP.panelsFor('llama').map(p => p.name);
    expect(llama).toContain('gen');
    expect(llama).toContain('details');
    expect(llama).not.toContain('requests');
    expect(HP.panelsFor('vllm').map(p => p.name)).toEqual(expect.arrayContaining(['requests', 'kv', 'gen']));
  });
  it('gives every panel a known kind', () => {
    const kinds = ['kv', 'ts', 'tiles', 'list', 'bars', 'split', 'table'];
    for (const p of HP.PANELS) expect(kinds).toContain(p.kind);
  });
});

describe('normalize', () => {
  it('flat and nested samples of one host agree', () => {
    const a = HP.normalize(LLAMA_FLAT), b = HP.normalize(LMS_MAC);
    expect(a.cpu).toBe(23.5);
    expect(b.cpu).toBe(a.cpu);
    expect(b.ram.percent).toBe(a.ram.percent);
    expect(b.disks).toEqual(a.disks);
    expect(a.disks[0]).toEqual({ mount: '/', percent: 40, used: 2e11, total: 5e11 });
    expect(a.net).toEqual({ sent: 2048, recv: 4096 });
    expect(a.cores).toEqual([10, 37]);
  });
  it('reads the system-only route payload', () => {
    const n = HP.normalize(HOST_ONLY);
    expect(n.cpu).toBe(23.5);
    expect(n.gpu).toBeNull();
  });
  it('no gpu gives gpu null and hasGpu false', () => {
    const n = HP.normalize(LMS_MAC);
    expect(n.gpu).toBeNull();
    expect(HP.hasGpu(n)).toBe(false);
    expect(HP.hasGpu(HP.normalize(LLAMA_FLAT))).toBe(true);
    expect(n.macPower.gpuBusy).toBe(64);
    expect(n.macPower.thermal).toBe('Fair');
  });
  it('parses liquidctl AIO and PSU values', () => {
    const n = HP.normalize(LLAMA_FLAT);
    expect(n.aio.temp).toBe(31.25);
    expect(n.aio.name).toBe('Kraken X63');
    expect(n.psu.totalOut).toBe(410.4);
    expect(n.psu.inputPower).toBe(455);
    expect(n.psu.efficiency).toBe('n/a');
    expect(n.fans).toHaveLength(1);
    expect(n.iscsi).toEqual({ state: 'LOGGED_IN', target: 'lun1' });
    const aio = HP.details('llama', 'aio', n, null);
    expect(aio.slice(0, 2)).toEqual([['Name', 'Kraken X63'], ['Liquid °C', '31.3']]);
    expect(HP.details('llama', 'psu', n, null).find(r => r[0] === 'Efficiency %')[1]).toBe('n/a');
  });
  it('an empty sample gives nulls without throwing', () => {
    for (const raw of [{}, null, undefined]) {
      const n = HP.normalize(raw);
      expect(n.cpu).toBeNull();
      expect(n.gpu).toBeNull();
      expect(n.macPower).toBeNull();
      expect(n.iscsi).toBeNull();
      expect(n.ups).toBeNull();
      expect(n.aio).toBeNull();
      expect(n.psu).toBeNull();
      expect(n.fans).toBeNull();
      expect(n.ram.percent).toBeNull();
      expect(n.net).toEqual({ sent: null, recv: null });
      expect(n.disks).toEqual([]);
      expect(n.ts).toBeNull();
    }
  });
});

describe('tiles', () => {
  it('an offline host reads Host offline and dashes', () => {
    const t = HP.tiles('llama', HP.normalize(LLAMA_FLAT), { live: LLAMA_FLAT, now: NOW }, false);
    expect(t[0]).toMatchObject({ label: 'State', value: 'Host offline' });
    expect(t.slice(1).every(x => x.value === '—')).toBe(true);
  });
  it('the host page has no Gen tokens/s tile', () => {
    const t = HP.tiles('host', HP.normalize(HOST_ONLY), null, true);
    expect(t.map(x => x.label)).toEqual(['State', 'CPU', 'Memory']);
    expect(t[1].value).toBe('24%');
  });
  it('GPU tiles appear only with a GPU', () => {
    const withGpu = HP.tiles('vllm', HP.normalize(VLLM), { live: VLLM, now: NOW }, true).map(x => x.label);
    expect(withGpu).toEqual(['State', 'CPU', 'Memory', 'GPU busy', 'VRAM', 'Prompt tokens/s', 'Gen tokens/s']);
    const mac = HP.tiles('lms', HP.normalize(LMS_MAC), { live: LMS_MAC, now: NOW }, true);
    expect(mac.map(x => x.label)).toEqual(['State', 'CPU', 'Memory', 'Prompt tokens/s', 'Gen tokens/s']);
    expect(mac[4].value).toBe('12.5');
  });
  it('provider pages show Prompt tokens/s right before Gen tokens/s, from the Server details prompt rate; a dash when missing', () => {
    const inf = { live: LLAMA_FLAT, now: NOW };
    const t = HP.tiles('llama', HP.normalize(LLAMA_FLAT), inf, true), at = t.findIndex(x => x.label === 'Prompt tokens/s');
    expect(t[at + 1].label).toBe('Gen tokens/s');
    const detail = HP.details('llama', 'details', HP.normalize(LLAMA_FLAT), inf).find(r => r[0] === 'Prompt tokens/s')[1];
    expect(detail.startsWith(t[at].value)).toBe(true);
    const none = HP.tiles('llama', HP.normalize({ llama: {} }), { live: { llama: {} }, now: NOW }, true).find(x => x.label === 'Prompt tokens/s');
    expect(none).toMatchObject({ value: '—', sub: '' });
  });
});

describe('details', () => {
  it('llama details has the 18 old server values', () => {
    const d = HP.details('llama', 'details', HP.normalize(LLAMA_FLAT), { live: LLAMA_FLAT, now: NOW });
    expect(d).toHaveLength(18);
    expect(d[1]).toEqual(['Gen tokens/s', '42.8 · live']);
    expect(d.find(r => r[0] === 'KV cache usage %')[1]).toBe('25.0%');
    expect(d.find(r => r[0] === 'Modalities')[1]).toBe('vision');
    expect(d.find(r => r[0] === 'Decode calls')[1]).toBe('—');
  });
  it('a missing value is a dash', () => {
    const d = HP.details('llama', 'gpu-details', HP.normalize({ gpu: { temperature_c: 50 } }), null);
    expect(d.find(r => r[0] === '°C edge')[1]).toBe('50.0');
    expect(d.filter(r => r[0] !== '°C edge').every(r => r[1] === '—')).toBe(true);
  });
  it('gpu details carry the old labels', () => {
    const d = HP.details('llama', 'gpu-details', HP.normalize(LLAMA_FLAT), null);
    const labels = d.map(r => r[0]);
    expect(labels).toEqual(expect.arrayContaining(['°C junction', 'SCLK MHz', 'Power profile', 'VRAM MB', 'GTT MB']));
    expect(d.find(r => r[0] === 'GTT MB')[1]).toBe('100 / 1,024');
  });
  it('memory, CPU, UPS and vLLM requests', () => {
    const n = HP.normalize(LLAMA_FLAT);
    expect(HP.details('llama', 'mem-details', n, null).map(r => r[0]))
      .toEqual(expect.arrayContaining(['Cached', 'Buffers', 'Swap used', 'Swap free']));
    expect(HP.details('llama', 'cpu-details', n, null)[0]).toEqual(['Temperature', '51.2°C']);
    const ups = HP.details('llama', 'ups', n, null);
    expect(ups[0]).toEqual(['Battery %', '98%']);
    expect(ups[5]).toEqual(['Status', 'Charged']);
    expect(HP.details('vllm', 'requests', HP.normalize(VLLM), VLLM)).toEqual([['Running', '2'], ['Waiting', '1']]);
  });
});

describe('series', () => {
  const at = (minAgo, extra) => Object.assign({ ts: iso(minAgo * MIN) }, extra);
  it('6 h has 360 one-minute buckets and 24 h has 288 five-minute buckets', () => {
    const rows = [at(1, { cpu_total: 10, ram_percent: 20 })];
    const s6 = HP.series('host', 'cpu-mem', rows, 6, NOW);
    expect(s6.labels).toHaveLength(360);
    expect(s6.series.map(s => s.name)).toEqual(['CPU %', 'Memory %']);
    expect(s6.series.map(s => s.slot)).toEqual([1, 2]);
    expect(HP.series('host', 'cpu-mem', rows, 24, NOW).labels).toHaveLength(288);
  });
  it('holds a 2-bucket gap and leaves a 5-bucket gap empty', () => {
    const rows = [at(10, { cpu_total: 5 }), at(7, { cpu_total: 9 }), at(1, { cpu_total: 3 })];
    const v = HP.series('host', 'cpu-mem', rows, 6, NOW).series[0].values;
    const n = v.length;
    expect(v[n - 11]).toBe(5);
    expect(v[n - 10]).toBe(5);
    expect(v[n - 9]).toBe(5);
    expect(v[n - 8]).toBe(9);
    expect(v.slice(n - 7, n - 2)).toEqual([null, null, null, null, null]);
    expect(v[n - 2]).toBe(3);
  });
  it('genSeries gives generated and prompt tokens per hour over the range, null for an hour with no row', () => {
    const hr = 3600000, cur = Math.floor(NOW / hr) * hr / 1000;
    const rows = [{ hour_ts: cur, tokens_gen: 40, tokens_prompt: 300 }, { hour_ts: cur - 3600, tokens_gen: 0, tokens_prompt: 0 },
      { hour_ts: cur - 2 * 3600, tokens_gen: 900 }, { hour_ts: cur - 6 * 3600, tokens_gen: 5, tokens_prompt: 5 }];
    const c = HP.genSeries(rows, 6, NOW);
    expect(c.labels).toHaveLength(6);
    expect(c.series).toEqual([{ name: 'Generated', values: [null, null, null, 900, 0, 40], slot: 1 },
      { name: 'Prompt', values: [null, null, null, 0, 0, 300], slot: 2 }]);
    expect(HP.genSeries(null, 12, NOW).series[0].values).toEqual(new Array(12).fill(null));
    expect(HP.chartStyle('gen')).toMatchObject({ mode: 'stack', idleLine: true, idle: 'Idle · no tokens in this range' });
    expect(HP.chartStyle('gen').barFill).toBe(0.8);
  });
  it('the Tokens chart links each provider page\'s stored token counters; LM Studio links the gateway ones', () => {
    const at = (page) => HP.metricLinks(page, 'gen', []).map(l => l.source + '/' + l.name);
    expect(at('llama')).toEqual(['llama/total_tokens_generated', 'llama/total_tokens_prompted']);
    expect(at('lms')).toEqual(['gateway/lms_gen_tokens_total', 'gateway/lms_prompt_tokens_total']);
    expect(at('vllm')).toEqual(['vllm/total_tokens_generated', 'vllm/total_tokens_prompted']);
    expect(HP.metricLinks('lms', 'gen', [])[0].label).toBe('View gateway tokens generated in metrics');
  });
  it('gpu-busy has GTT only when a row has gpu_gtt', () => {
    const plain = [at(1, { gpu_util: 50, gpu_vram: 60 })];
    expect(HP.series('llama', 'gpu-busy', plain, 6, NOW).series.map(s => s.name)).toEqual(['GPU busy %', 'VRAM %']);
    const gtt = plain.concat([at(2, { gpu_gtt: 4 })]);
    expect(HP.series('llama', 'gpu-busy', gtt, 6, NOW).series.map(s => s.name)).toEqual(['GPU busy %', 'VRAM %', 'GTT %']);
  });
  it('tps follows the page fields; disk adds iSCSI only when reported', () => {
    const rows = [at(1, { lms_tps: 7, lms_pps: 9, llama_tps: 99, disk_root_pct: 40 })];
    const tps = HP.series('lms', 'tps', rows, 6, NOW).series;
    expect(tps.map(s => s.values[s.values.length - 2])).toEqual([9, 7]);
    expect(HP.series('host', 'disk', rows, 6, NOW).series.map(s => s.name)).toEqual(['/']);
    expect(HP.series('host', 'nope', rows, 6, NOW).series).toEqual([]);
  });
});

describe('liveRow', () => {
  it('builds a history-shaped row that merges', () => {
    const r = HP.liveRow('llama', HP.normalize(LLAMA_FLAT), { live: LLAMA_FLAT }, NOW);
    expect(r.ts).toBe(new Date(NOW).toISOString());
    expect(r).toMatchObject({ cpu_total: 23.5, ram_percent: 31.2, gpu_util: 88, gpu_gtt: 3, net_sent: 2048,
      io_write: 3000, disk_root_pct: 40, disk_iscsi_pct: 50, aio_temp: 31.25, psu_out: 410.4, psu_in: 455,
      llama_tps: 42.75, llama_gen_tokens: 12000 });
    expect(HV.mergeRows([], [r], null)).toHaveLength(1);
    const v = HP.liveRow('vllm', HP.normalize(VLLM), VLLM, NOW);
    expect(v).toMatchObject({ vllm_tps: 55.5, vllm_pps: 120, vllm_kv: 33.3 });
    expect(HP.liveRow('lms', HP.normalize(LMS_MAC), LMS_MAC, NOW)).toMatchObject({ lms_tps: 12.5, mac_gpu_busy: 64 });
  });
});

describe('serverPanel', () => {
  it('llama reuses the Overall builder for one host', () => {
    const p = HP.serverPanel('llama', { live: LLAMA_FLAT, state: { aggregate: 'active', model: 'org/qwen', host: 'tower.local' },
      models: { data: [{ id: 'org/qwen', status: { value: 'loaded', args: ['--ctx-size', '32768'] } }] } }, null, true);
    expect(p.state.label).toBe('Model loaded');
    expect(p.kv[0]).toEqual(['Model', 'qwen']);
    expect(p.note).toBe('tower');
    expect(p.table.rows.some(r => r[0] === 'Hosts awake')).toBe(false);
    expect(OV.llamaPanel).toBeTypeOf('function');
  });
  it('an offline host reads Host offline', () => {
    expect(HP.serverPanel('llama', { state: { aggregate: 'active' } }, null, false).state.label).toBe('Host offline');
    expect(HP.serverPanel('lms', LMS_MAC, null, false).state.label).toBe('Host offline');
    expect(HP.serverPanel('vllm', VLLM, null, false).state.label).toBe('Host offline');
  });
  it('lms lists installed models with their status', () => {
    const p = HP.serverPanel('lms', LMS_MAC, null, true);
    expect(p.state.label).toBe('Server on');
    expect(p.models.map(m => m.label)).toEqual(['llama-3', 'mistral']);
    expect(p.models[0]).toMatchObject({ status: 'Generating', tone: 'ok', sub: '4 GB' });
    expect(p.models[1]).toMatchObject({ status: 'Idle', tone: 'off' });
  });
  it('vllm builds from the vLLM metrics payload', () => {
    const p = HP.serverPanel('vllm', VLLM, null, true);
    expect(p.state).toMatchObject({ label: 'Server on', tone: 'ok' });
    expect(p.kv).toEqual(expect.arrayContaining([['Model', 'mistral-7b'], ['Requests', '2 running · 1 waiting'], ['KV cache', '33% full']]));
    expect(HP.serverPanel('host', null, null, true)).toBeNull();
  });
});

describe('thresholdsFor', () => {
  const rule = (over) => Object.assign({ rule_id: 'r1', name: 'CPU hot', enabled: true, metric_source: 'system',
    metric_name: 'cpu_total', rule_type: 'threshold_above', severity: 'critical', source_host: 'h1',
    config: { threshold: { value: 90 } } }, over);
  it('matches an enabled rule on its host only', () => {
    expect(HP.thresholdsFor([rule()], 'system', 'cpu_total', 'h1')).toEqual([{ value: 90, label: '', tone: 'bad' }]);
    expect(HP.thresholdsFor([rule()], 'system', 'cpu_total', 'h2')).toEqual([]);
    expect(HP.thresholdsFor([rule({ source_host: null, severity: 'warning' })], 'system', 'cpu_total', 'h2'))
      .toEqual([{ value: 90, label: '', tone: 'warn' }]);
  });
  it('skips a disabled rule', () => {
    expect(HP.thresholdsFor([rule({ enabled: false })], 'system', 'cpu_total', 'h1')).toEqual([]);
  });
  it('draws both ends of a range rule', () => {
    const r = rule({ rule_type: 'threshold_range', severity: 'info', name: '', config: { threshold: { lower: 10, upper: 80 } } });
    expect(HP.thresholdsFor([r], 'system', 'cpu_total', 'h1'))
      .toEqual([{ value: 10, label: '', tone: 'muted' }, { value: 80, label: '', tone: 'muted' }]);
  });
});

describe('mapLegacyLayout', () => {
  it('maps old ids once, dropping unknown ones and the old grid keys', () => {
    const lay = { overallBorrowed: ['gpu', 'lms-cpu', 'vllm-kv', 'llama-throughput', 'gone'],
      cardSizes: { 'ov-borrow-gpu': 'w2', gpu: 'w1' }, overallOrder: ['ov-borrow-gpu', 'ov-tiles'],
      order: ['gpu'], hidden: [], lmsCols: 3, vllmHidden: ['vllm-io'],
      hiddenByAgent: { llama: { a: [] }, lms: {} }, orderByAgent: { vllm: {} }, sizesByAgent: { llama: {} } };
    expect(HP.mapLegacyLayout(lay)).toEqual({ changed: true });
    expect(lay.overallBorrowed).toEqual(['llama-gpu-busy', 'lms-cpu-mem', 'vllm-kv', 'llama-tps']);
    expect(lay.cardSizes).toEqual({ 'ov-borrow-llama-gpu-busy': 'w2', 'llama-gpu-busy': 'w1' });
    expect(lay.overallOrder).toEqual(['ov-borrow-llama-gpu-busy', 'ov-tiles']);
    for (const k of ['order', 'hidden', 'lmsCols', 'vllmHidden', 'hiddenByAgent', 'orderByAgent', 'sizesByAgent'])
      expect(k in lay).toBe(false);
    expect(lay.hostPageMapped).toBe(1);
    const snap = JSON.stringify(lay);
    expect(HP.mapLegacyLayout(lay)).toEqual({ changed: false });
    expect(JSON.stringify(lay)).toBe(snap);
  });
  it('keeps pinned manager cards and new panel ids, and dedupes', () => {
    const lay = { overallBorrowed: ['services', 'gpu', 'llama-gpu-busy', 'host-cpu-mem', 'influxdb'],
      overallOrder: ['ov-borrow-services', 'ov-borrow-influxdb'] };
    HP.mapLegacyLayout(lay, ['services']);
    expect(lay.overallBorrowed).toEqual(['services', 'llama-gpu-busy', 'host-cpu-mem']);
    expect(lay.overallOrder).toEqual(['ov-borrow-services']);
    const bare = { overallBorrowed: ['services'] };
    HP.mapLegacyLayout(bare);
    expect(bare.overallBorrowed).toEqual([]);
  });
  it('maps every legacy id to a panel of its page', () => {
    for (const [old, id] of Object.entries(HP.LEGACY_IDS)) {
      if (id == null) continue;
      const [page, ...rest] = id.split('-');
      expect(HP.panelsFor(page).map(p => p.name), old).toContain(rest.join('-'));
    }
  });
});

describe('fix round 1', () => {
  const small = { system: { disk: [{ mountpoint: '/', total_bytes: 5e11, percent: 40 },
                                   { mountpoint: '/boot/efi', total_bytes: 50e6, percent: 5 }] } };
  it('drops small mounts only on the LM Studio and vLLM pages', () => {
    expect(HP.normalize(small, { page: 'llama' }).disks.map(d => d.mount)).toEqual(['/', '/boot/efi']);
    expect(HP.normalize(small, { page: 'host' }).disks.map(d => d.mount)).toEqual(['/', '/boot/efi']);
    expect(HP.normalize(small).disks).toHaveLength(2);
    expect(HP.normalize(small, { page: 'lms' }).disks.map(d => d.mount)).toEqual(['/']);
    expect(HP.normalize(small, { page: 'vllm' }).disks.map(d => d.mount)).toEqual(['/']);
  });
  it('llama details carry slot and request peaks', () => {
    const live = Object.assign({}, LLAMA_FLAT, { llama: Object.assign({}, LLAMA_FLAT.llama,
      { active_slots: 3, requests_processing: 0, requests_deferred: 0 }) });
    const peaks = { active_slots: { val: 4, ts: iso(3 * MIN) }, requests_processing: { val: 2, ts: iso(3 * MIN) },
                    requests_deferred: { val: null, ts: null } };
    const d = HP.details('llama', 'details', HP.normalize(live), { live, peaks, now: NOW });
    const get = k => d.find(r => r[0] === k)[1];
    expect(get('Active slots')).toBe('3');
    expect(get('Requests processing')).toBe('0 (peak 2 3m ago)');
    expect(get('Requests deferred')).toBe('0');
    const noTs = { live, peaks: { requests_deferred: { val: 1, ts: null } }, now: NOW };
    expect(HP.details('llama', 'details', null, noTs).find(r => r[0] === 'Requests deferred')[1]).toBe('0 (peak 1)');
    const zero = { live, peaks: { requests_deferred: { val: 0, ts: iso(MIN) } }, now: NOW };
    expect(HP.details('llama', 'details', null, zero).find(r => r[0] === 'Requests deferred')[1]).toBe('0');
  });
  it('window ratios keep the old tooltip counts', () => {
    const live = { llama: { cache_reuse: { ratio: 0.62, state: 'ok', cached: 1240, processed: 2000 },
                            spec_accept: { ratio: null, state: 'off', accepted: 0, drafted: 0 } } };
    const d = HP.details('llama', 'details', null, { live, now: NOW });
    expect(d.find(r => r[0] === 'Prompt cache reuse')[1]).toBe('62.0% (1,240 of 2,000)');
    expect(d.find(r => r[0] === 'Draft tokens accepted')[1]).toBe('Not in use');
    const idle = { llama: { cache_reuse: { ratio: 0.5, state: 'idle', cached: 5, processed: null } } };
    expect(HP.details('llama', 'details', null, { live: idle, now: NOW }).find(r => r[0] === 'Prompt cache reuse')[1]).toBe('50.0%');
  });
  it('rate values carry the window peak and mode like the old tiles', () => {
    const live = Object.assign({}, LLAMA_FLAT, { throughput_window: {
      gen: { avg: 20, peak: { v: 60, ts: iso(5 * MIN) } }, prompt: { avg: 100, peak: { v: 500, ts: iso(2 * MIN) } } } });
    const inf = { live, now: NOW };
    const d = HP.details('llama', 'details', HP.normalize(live), inf);
    expect(d[0]).toEqual(['Prompt tokens/s', '300.0 · live · peak 500.0 · 2m ago']);
    expect(d[1]).toEqual(['Gen tokens/s', '42.8 · live · peak 60.0 · 5m ago']);
    const tile = HP.tiles('llama', HP.normalize(live), inf, true).find(t => t.label === 'Gen tokens/s');
    expect(tile).toMatchObject({ value: '42.8', sub: 'live · peak 60.0 · 5m ago' });
    const idle = Object.assign({}, live, { llama: { tokens_per_second: 0 } });
    expect(HP.tiles('llama', HP.normalize(idle), { live: idle, now: NOW }, true).find(t => t.label === 'Gen tokens/s'))
      .toMatchObject({ value: '20.0', sub: '60‑min avg · peak 60.0 · 5m ago' });
    const tracker = { stats: () => ({ peak: { v: 9, t: NOW - 60000 }, avg: 4 }) };
    const lmsInf = { live: LMS_MAC, peaks: { tps: tracker }, now: NOW };
    expect(HP.details('lms', 'details', HP.normalize(LMS_MAC), lmsInf)[1]).toEqual(['Gen tokens/s', '12.5 · live · peak 9.0 · 1m ago']);
  });
  it('the chat template cell shows its size, a 600-character preview as its title and the full text for the table', () => {
    const tmpl = '{% for m in messages %}{{ m.content }}{% endfor %}'.repeat(30);
    const live = { llama: { chat_template_len: 2048, chat_template: tmpl } };
    const c = HP.details('llama', 'details', null, { live, now: NOW }).find(r => r[0] === 'Chat template')[1];
    expect(c).toEqual({ text: '2.0 KB', v: '2.0 KB', title: tmpl.slice(0, 600), full: '2.0 KB · ' + tmpl });
    expect(c.title).toHaveLength(600);
    const sizeOnly = HP.details('llama', 'details', null, { live: { llama: { chat_template_len: 1024 } }, now: NOW });
    expect(sizeOnly.find(r => r[0] === 'Chat template')[1]).toBe('1.0 KB');
  });
  it('liveNotes gives the network and disk rates', () => {
    expect(HP.liveNotes(HP.normalize(LLAMA_FLAT))).toEqual({ net: '2.0 KB/s out · 4.0 KB/s in', io: '1000 B/s read · 2.9 KB/s write' });
    expect(HP.liveNotes(HP.normalize(null))).toEqual({ net: '— out · — in', io: '— read · — write' });
  });
  it('formats counts the same in any locale', () => {
    const d = HP.details('llama', 'details', null, { live: { llama: { total_tokens_generated: 1234567 } }, now: NOW });
    expect(d.find(r => r[0] === 'Tokens generated')[1]).toBe('1,234,567');
  });
  it('an unsupported range reads as 24 h', () => {
    const rows = [{ ts: iso(MIN), cpu_total: 10 }];
    expect(HP.series('host', 'cpu-mem', rows, 7, NOW)).toEqual(HP.series('host', 'cpu-mem', rows, 24, NOW));
    expect(HP.series('host', 'cpu-mem', rows, 7, NOW).labels).toHaveLength(288);
  });
  it('details are dashes for an offline host', () => {
    const n = HP.normalize(LLAMA_FLAT);
    for (const name of ['details', 'gpu-details', 'cpu-details', 'mem-details', 'ups', 'aio', 'psu']) {
      const d = HP.details('llama', name, n, { live: LLAMA_FLAT, now: NOW }, false);
      expect(d.length, name).toBeGreaterThan(0);
      expect(d.every(r => r[1] === '—'), name).toBe(true);
    }
    expect(HP.details('llama', 'aio', n, null, true)[1][1]).toBe('31.3');
  });
  it('first paint before any fetch does not throw', () => {
    for (const page of ['llama', 'lms', 'vllm', 'host']) {
      expect(() => HP.tiles(page, HP.normalize(null), { live: {}, now: NOW }, true)).not.toThrow();
      expect(() => HP.tiles(page, HP.normalize(null), null, true)).not.toThrow();
      expect(() => HP.serverPanel(page, null, HP.normalize(null), true)).not.toThrow();
      expect(() => HP.details(page, 'details', HP.normalize(null), null, true)).not.toThrow();
    }
    expect(HP.serverPanel('llama', null, null, true).state.label).toBe('Nothing reported yet');
  });
});

describe('fix wave', () => {
  it('GPU details, liquid cooler and power supply show their names; GPU details show edge °C and W draw', () => {
    const n = HP.normalize(LLAMA_FLAT);
    const g = HP.details('llama', 'gpu-details', n, null);
    expect(g.slice(0, 3)).toEqual([['Name', 'RX 7900'], ['°C edge', '60.0'], ['W draw', '250.0']]);
    expect(HP.details('llama', 'aio', n, null)[0]).toEqual(['Name', 'Kraken X63']);
    expect(HP.details('llama', 'psu', n, null)[0]).toEqual(['Name', 'HX1000i']);
    expect(HP.details('llama', 'gpu-details', n, null, false).slice(0, 3).every(r => r[1] === '—')).toBe(true);
  });
  it('the fan table carries the fan controller name', () => {
    const t = HP.fanTable(HP.normalize(LLAMA_FLAT));
    expect(t.name).toBe('Smart Device');
    expect(t.rows).toHaveLength(1);
    expect(HP.fanTable(HP.normalize(null)).name).toBeNull();
  });
  it('memory and VRAM charts list their alarm metrics with CPU and GPU busy', () => {
    expect(HP.CHART_METRIC['cpu-mem']).toEqual([['system', 'cpu_total'], ['system', 'ram_percent']]);
    expect(HP.CHART_METRIC['gpu-busy']).toEqual([['system', 'gpu_gpu_util_percent'], ['system', 'gpu_vram_usage_percent']]);
    expect(HP.CHART_METRIC.disk).toEqual([['system', 'disk_root_percent']]);
    expect(HP.CHART_METRIC['mac-gpu']).toEqual([['mac_power', 'gpu_busy_pct']]);
  });
  it('throughput takes each bucket\'s peak; other charts keep the mean', () => {
    const at = (secAgo, extra) => Object.assign({ ts: new Date(NOW - 60000 - secAgo * 1000).toISOString() }, extra);
    const rows = [at(10, { llama_tps: 10, llama_pps: 100, cpu_total: 10 }), at(20, { llama_tps: 40, llama_pps: 50, cpu_total: 30 }),
      at(25, { llama_tps: 0, llama_pps: 0, cpu_total: 50 })];
    const last = (s) => s.values[s.values.length - 2];
    const tps = HP.series('llama', 'tps', rows, 6, NOW).series;
    expect(tps.map(s => s.name)).toEqual(['Prompt t/s', 'Gen t/s']);
    expect(tps.map(s => s.slot)).toEqual([2, 1]);
    expect(tps.map(last)).toEqual([100, 40]);
    expect(last(HP.series('llama', 'cpu-mem', rows, 6, NOW).series[0])).toBe(30);
    expect(last(HP.series('lms', 'tps', [at(5, { lms_tps: 3 }), at(15, { lms_tps: 9 })], 6, NOW).series[1])).toBe(9);
  });
  it('peak-bar charts take each bucket\'s max with its mean as avg, in coarser buckets and with no held gaps', () => {
    const at = (minAgo, extra) => Object.assign({ ts: iso(minAgo * MIN) }, extra);
    const rows = [at(50, { mac_gpu_busy: 10 }), at(47, { mac_gpu_busy: 30 }), at(2, { mac_gpu_busy: 0, llama_tps: 8 }), at(1, { mac_gpu_busy: 4, llama_tps: 2 })];
    const g = HP.series('lms', 'mac-gpu', rows, 6, NOW);
    expect(g.labels).toHaveLength(72);
    expect(HP.series('lms', 'mac-gpu', rows, 12, NOW).labels).toHaveLength(72);
    expect(HP.series('llama', 'tps', rows, 24, NOW).labels).toHaveLength(96);
    const s = g.series[0], n = s.values.length;
    // 12:00:30 now: 11:10:30 and 11:13:30 share the 11:10 bucket; 11:58:30 and 11:59:30 share 11:55.
    expect(s.values[n - 11]).toBe(30);
    expect(s.avg[n - 11]).toBe(20);
    expect(s.values.slice(n - 10, n - 2)).toEqual(new Array(8).fill(null));
    expect(s.values[n - 2]).toBe(4);
    expect(s.avg[n - 2]).toBe(2);
    const t = HP.series('llama', 'tps', rows, 24, NOW).series.find(x => x.name === 'Gen t/s');
    expect(t.avg[t.avg.length - 2]).toBe(5);
    expect(HP.series('llama', 'cpu-mem', [at(1, { cpu_total: 3 })], 6, NOW).series[0].avg).toBeUndefined();
  });
  it('metric links name each shown series with a stored metric, in its page\'s fields', () => {
    const at = (minAgo, extra) => Object.assign({ ts: iso(minAgo * MIN) }, extra);
    expect(HP.metricLinks('llama', 'cpu-mem', [])).toEqual([
      { label: 'View CPU % in metrics', source: 'system', name: 'cpu_total' },
      { label: 'View Memory % in metrics', source: 'system', name: 'ram_percent' }]);
    expect(HP.metricLinks('lms', 'tps', []).map(l => l.name)).toEqual(['lms_prompt_tokens_per_second', 'lms_tokens_per_second']);
    expect(HP.metricLinks('llama', 'net', []).map(l => l.label)).toEqual(['View network out in metrics', 'View network in in metrics']);
    expect(HP.metricLinks('llama', 'gpu-busy', []).map(l => l.name)).toEqual(['gpu_gpu_util_percent', 'gpu_vram_usage_percent']);
    expect(HP.metricLinks('llama', 'gpu-busy', [at(1, { gpu_gtt: 3 })]).map(l => l.name)).toContain('gpu_gtt_usage_percent');
    expect(HP.metricLinks('vllm', 'tps', [])).toEqual([]);
    expect(HP.metricLinks('host', 'psu', [])).toEqual([]);
  });
  it('chart styles: split peak bars with an idle message for throughput, peak bars for Mac GPU busy, a busy strip over steps for GPU busy', () => {
    expect(HP.chartStyle('tps')).toEqual({ bars: 'peak', split: true, idle: 'Idle · no requests in this range' });
    expect(HP.chartStyle('mac-gpu')).toEqual({ bars: 'peak' });
    expect(HP.chartStyle('gpu-busy')).toEqual({ strip: 'GPU busy %', stripLabel: 'Busy', step: true });
    expect(HP.chartStyle('cpu-mem')).toEqual({});
    HP.chartStyle('tps').bars = 'x';
    expect(HP.chartStyle('tps').bars).toBe('peak');
  });
  it('old hidden lists and every host\'s hidden list map once into each page\'s hidden panels', () => {
    const lay = { hidden: ['ups', 'gpu', 'gone', 'services'], lmsHidden: ['lms-power', 'lms-cpu'], vllmHidden: ['vllm-kv'],
      hiddenByAgent: { llama: { a: ['aio'], b: ['ups', 'psu'] }, lms: { a: ['lms-models'] }, vllm: { c: ['vllm-io', 7] } },
      llamaPanelsHidden: ['llama-net'] };
    HP.mapLegacyLayout(lay, ['services']);
    expect(lay.llamaPanelsHidden).toEqual(['llama-net', 'llama-ups', 'llama-gpu-busy', 'llama-aio', 'llama-psu']);
    expect(lay.lmsPanelsHidden).toEqual(['lms-cpu-mem', 'lms-models']);
    expect(lay.vllmPanelsHidden).toEqual(['vllm-kv', 'vllm-io']);
    expect(lay.hostPanelsHidden).toBeUndefined();
    for (const k of ['hidden', 'lmsHidden', 'vllmHidden', 'hiddenByAgent']) expect(k in lay, k).toBe(false);
    expect(HP.HIDDEN_KEYS).toEqual({ llama: 'llamaPanelsHidden', lms: 'lmsPanelsHidden', vllm: 'vllmPanelsHidden', host: 'hostPanelsHidden' });
    const bare = { hidden: [] };
    HP.mapLegacyLayout(bare);
    expect('llamaPanelsHidden' in bare).toBe(false);
  });
});

describe('live-check fixes', () => {
  const at = (minAgo, extra) => Object.assign({ ts: iso(minAgo * MIN) }, extra);
  const last = (s) => s.values[s.values.length - 2];
  it('the tiles panel is titled "Snapshot" in the Host row', () => {
    expect(HP.PANELS.find(p => p.name === 'tiles')).toMatchObject({ row: 'tiles', title: 'Snapshot' });
    expect(HP.ROWS[0]).toEqual(['tiles', 'Host']);
  });
  it('threshold lines carry no label', () => {
    const r = { enabled: true, name: 'Memory full', metric_source: 'system', metric_name: 'ram_percent', rule_type: 'threshold_range',
      severity: 'critical', config: { threshold: { lower: 10, upper: 95 } } };
    expect(HP.thresholdsFor([r], 'system', 'ram_percent', 'h1')).toEqual([{ value: 10, label: '', tone: 'bad' }, { value: 95, label: '', tone: 'bad' }]);
  });
  it('network and disk activity use one unit per chart from its largest value', () => {
    const net = (sent, recv) => HP.series('host', 'net', [at(1, { net_sent: sent, net_recv: recv })], 6, NOW).series;
    const b = net(500, 1000);
    expect(b.map(s => s.name)).toEqual(['Out (B/s)', 'In (B/s)']);
    expect(b.map(last)).toEqual([500, 1000]);
    const kb = net(512, 100 * 1024);
    expect(kb.map(s => s.name)).toEqual(['Out (KB/s)', 'In (KB/s)']);
    expect(kb.map(last)).toEqual([0.5, 100]);
    const io = HP.series('host', 'io', [at(1, { io_read: 500 * 1048576, io_write: 20 * 1048576 })], 6, NOW).series;
    expect(io.map(s => s.name)).toEqual(['Read (MB/s)', 'Write (MB/s)']);
    expect(io.map(last)).toEqual([500, 20]);
    const gb = HP.series('host', 'io', [at(1, { io_read: 3 * 1073741824 * 1024 })], 6, NOW).series;
    expect(gb[0].name).toBe('Read (GB/s)');
    expect(last(gb[0])).toBe(3072);
    expect(HP.series('host', 'net', [], 6, NOW).series.map(s => s.name)).toEqual(['Out (B/s)', 'In (B/s)']);
    expect(HP.series('host', 'cpu-mem', [at(1, { cpu_total: 5000 })], 6, NOW).series[0].name).toBe('CPU %');
  });
});

describe('vLLM server panel', () => {
  it('shows State once; the server\'s own state word stays in Server details', () => {
    const down = { agent_online: true, vllm: { state: 'down' } };
    const p = HP.serverPanel('vllm', down, null, true);
    expect(p.state.label).toBe('Server off');
    expect(p.kv.map(r => r[0])).toEqual(['Model']);
    expect(p.table.rows.filter(r => r[0] === 'State')).toEqual([['State', 'Server off']]);
    expect(HP.details('vllm', 'details', null, down, true).find(r => r[0] === 'State')[1]).toBe('down');
    const on = HP.serverPanel('vllm', VLLM, null, true);
    expect(on.kv.filter(r => r[0] === 'State')).toEqual([]);
  });
});

describe('operator fixes', () => {
  it('a rate with no value reads a dash alone, with no mode or peak', () => {
    const live = { llama: { tokens_per_second: null, prompt_tokens_per_second: null } };
    const peaks = { tps: { stats: () => ({ peak: { v: 9, t: NOW - MIN }, avg: null }) }, pps: { stats: () => ({ peak: null, avg: null }) } };
    const inf = { live, peaks, now: NOW };
    const d = HP.details('llama', 'details', HP.normalize(live), inf);
    expect(d.find(r => r[0] === 'Gen tokens/s')[1]).toBe('—');
    expect(d.find(r => r[0] === 'Prompt tokens/s')[1]).toBe('—');
    const tile = HP.tiles('llama', HP.normalize(live), inf, true).find(t => t.label === 'Gen tokens/s');
    expect(tile.value).toBe('—');
    expect(tile.sub).toBe('');
    const lms = { gateway_rates: {} };
    expect(HP.details('lms', 'details', HP.normalize(lms), { live: lms, now: NOW }).slice(0, 2).map(r => r[1])).toEqual(['—', '—']);
    // A value still shows its mode and peak.
    const on = HP.details('llama', 'details', HP.normalize(LLAMA_FLAT), { live: LLAMA_FLAT, peaks, now: NOW });
    expect(on.find(r => r[0] === 'Gen tokens/s')[1]).toBe('42.8 · live · peak 9.0 · 1m ago');
  });

  const MAC_DISKS = ['/', '/System/Volumes/VM', '/System/Volumes/Preboot', '/System/Volumes/Update', '/System/Volumes/xarts',
    '/System/Volumes/iSCPreboot', '/System/Volumes/Hardware', '/System/Volumes/Data', '/private/var/run/com.apple.security.cryptexd/mnt/a1',
    '/Library/Developer/CoreSimulator/Volumes/iOS_22A', '/Volumes/Backup', '/System/Volumes/Data/home']
    .map(m => ({ mountpoint: m, total_bytes: 5e11, used_bytes: 1e11, percent: 20 }));
  it('a Mac\'s Disk space bars show its data volumes only; every mount stays in the sample', () => {
    const mac = HP.normalize(Object.assign({}, LMS_MAC, { system: Object.assign({}, SYS, { disk: MAC_DISKS }) }), { page: 'lms' });
    expect(mac.mac).toBe(true);
    expect(mac.disks).toHaveLength(12);
    const bars = HP.diskBars(mac);
    expect(bars.map(d => d.mount)).toEqual(['/System/Volumes/Data', '/Volumes/Backup']);
    expect(bars[0].label).toBe('Macintosh HD (/)');
    // A Mac without its power block is still known by its system volumes.
    const bare = HP.normalize({ system: Object.assign({}, SYS, { disk: MAC_DISKS }) });
    expect(bare.mac).toBe(true);
    expect(HP.diskBars(bare)).toHaveLength(2);
    // Linux lists are unchanged, even for a mount under a macOS-like path.
    const lin = HP.normalize(Object.assign({}, LLAMA_FLAT, { disk: SYS.disk.concat([{ mountpoint: '/Library/Developer/CoreSimulator/x', total_bytes: 5e11, percent: 1 }]) }));
    expect(lin.mac).toBe(false);
    expect(HP.diskBars(lin).map(d => d.mount)).toEqual(['/', '/mnt/iscsi', '/Library/Developer/CoreSimulator/x']);
    // Linux pseudo and runtime mounts are left out; a look-alike name is kept.
    const sys = HP.normalize(Object.assign({}, LLAMA_FLAT, { disk: SYS.disk.concat(['/sys/class/drm', '/proc', '/dev/shm', '/run/user/1000', '/system',
      '/run/media/ann/USB', '/run/lock', '/run/media'].map(m => ({ mountpoint: m, total_bytes: 5e11, percent: 1 }))) }));
    expect(HP.diskBars(sys).map(d => d.mount)).toEqual(['/', '/mnt/iscsi', '/system', '/run/media/ann/USB']);
  });

  it('a Mac\'s memory details leave out Cached and Buffers; Linux keeps them', () => {
    const labels = (raw) => HP.details('lms', 'mem-details', HP.normalize(raw), null, true).map(r => r[0]);
    expect(labels(LMS_MAC)).toEqual(['Used', 'Available', 'Swap used', 'Swap free']);
    expect(labels(LLAMA_FLAT)).toEqual(['Used', 'Available', 'Cached', 'Buffers', 'Swap used', 'Swap free']);
    expect(HP.details('lms', 'mem-details', HP.normalize(LMS_MAC), null, false).map(r => r[0])).toEqual(['Used', 'Available', 'Swap used', 'Swap free']);
  });

  it('a hidden old LM Studio power card leaves the Mac power panel shown; a pinned one still maps to it', () => {
    const lay = { lmsHidden: ['lms-power', 'lms-network'], overallBorrowed: ['lms-power'] };
    expect(HP.mapLegacyLayout(lay)).toEqual({ changed: true });
    expect(lay.lmsPanelsHidden).toEqual(['lms-net']);
    expect(lay.overallBorrowed).toEqual(['lms-mac-power']);
    expect(lay.lmsMacPowerShown).toBe(1);
    expect(HP.mapLegacyLayout(lay)).toEqual({ changed: false });
  });

  it('a layout mapped before shows the Mac power panel again once; hiding it later holds', () => {
    const lay = { hostPageMapped: 1, lmsPanelsHidden: ['lms-net', 'lms-mac-power'], llamaPanelsHidden: ['llama-mac-power'] };
    expect(HP.mapLegacyLayout(lay)).toEqual({ changed: true });
    expect(lay.lmsPanelsHidden).toEqual(['lms-net']);
    expect(lay.llamaPanelsHidden).toEqual(['llama-mac-power']);
    expect(lay.lmsMacPowerShown).toBe(1);
    lay.lmsPanelsHidden.push('lms-mac-power');
    expect(HP.mapLegacyLayout(lay)).toEqual({ changed: false });
    expect(lay.lmsPanelsHidden).toEqual(['lms-net', 'lms-mac-power']);
    const none = { hostPageMapped: 1 };
    expect(HP.mapLegacyLayout(none)).toEqual({ changed: true });
    expect(none).toEqual({ hostPageMapped: 1, lmsMacPowerShown: 1 });
  });
});

describe('llama last-seen counters', () => {
  const AT = new Date(2026, 10, 16, 14, 32, 5).getTime();
  const SEEN = { ll: { total_tokens_generated: 12000, n_decode_total: 500, total_slots: 4, kv_cache_usage_ratio: 0.25 }, at: AT };
  const SLEEP = { llama: { tokens_per_second: null, active_slots: 0 } };
  const inf = (extra) => Object.assign({ live: SLEEP, state: { aggregate: 'sleeping' }, lastLlama: SEEN, now: NOW }, extra);
  const val = (d, k) => d.find(r => r[0] === k)[1];
  it('a sleeping server shows its last counters with a note; rates and slots stay live', () => {
    const d = HP.details('llama', 'details', HP.normalize(SLEEP), inf(), true);
    expect(val(d, 'Tokens generated')).toBe('12,000');
    expect(val(d, 'Decode calls')).toBe('500');
    expect(val(d, 'Total slots')).toBe('4');
    expect(val(d, 'KV cache usage %')).toBe('25.0%');
    expect(val(d, 'Gen tokens/s')).toBe('—');
    expect(val(d, 'Active slots')).toBe('0');
    expect(HP.lastSeenNote('llama', inf(), true)).toBe('Last seen 14:32 · server sleeping');
    expect(HP.lastSeenNote('llama', inf({ state: { aggregate: 'idle' } }), true)).toBe('Last seen 14:32 · server not reporting');
  });
  it('live counters, no last block or an offline host show no last-seen values', () => {
    expect(HP.lastSeenNote('llama', inf({ live: LLAMA_FLAT }), true)).toBe('');
    expect(val(HP.details('llama', 'details', null, inf({ live: LLAMA_FLAT }), true), 'Tokens generated')).toBe('12,000');
    expect(val(HP.details('llama', 'details', null, inf({ lastLlama: null }), true), 'Tokens generated')).toBe('—');
    expect(HP.lastSeenNote('llama', inf({ lastLlama: null }), true)).toBe('');
    expect(HP.details('llama', 'details', null, inf(), false).every(r => r[1] === '—')).toBe(true);
    expect(HP.lastSeenNote('llama', inf(), false)).toBe('');
    expect(HP.llamaHasCounters({ tokens_per_second: 3 })).toBe(false);
    expect(HP.llamaHasCounters({ total_slots: 4 })).toBe(true);
  });
});

describe('manager host page (#1432 phase 4d)', () => {
  it('has a mgr page with only the System row', () => {
    expect(HP.PAGES.mgr).toEqual({ provider: null, label: 'Manager host', sub: 'manager' });
    expect(HP.panelsFor('mgr').map(p => p.name)).toEqual(['cpu-mem', 'cpu-details', 'mem-details', 'net', 'io', 'disk']);
    expect(HP.panelsFor('mgr').every(p => p.row === 'sys')).toBe(true);
  });
  it('mapLegacyLayout keeps the manager page panel ids and still drops unknown ids', () => {
    const mgr = ['mgr-services', 'mgr-db', 'mgr-streams', 'mgr-hosts', 'mgr-cpu-mem', 'mgr-disk', 'mgr-net', 'mgr-procs',
      'mgr-tiles', 'mgr-perf', 'mgr-ae-perf', 'mgr-influx-perf', 'mgr-tower', 'mgr-io', 'mgr-cpu-details', 'mgr-mem-details'];
    const lay = { overallBorrowed: mgr.concat(['mgr-nope', 'gpu']),
      overallOrder: mgr.map(id => 'ov-borrow-' + id).concat(['ov-borrow-mgr-nope']) };
    HP.mapLegacyLayout(lay, []);
    expect(lay.overallBorrowed).toEqual(mgr.concat(['llama-gpu-busy']));
    expect(lay.overallOrder).toEqual(mgr.map(id => 'ov-borrow-' + id));
  });
});
