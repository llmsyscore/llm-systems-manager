// #565: pure view-model transforms for the Overall-tab fleet band.
// Fixtures captured from the live dev manager 2026-08-17 (trimmed); the
// alerts fixture is source-derived from models/alert.py to_dict (none active).
import { describe, it, expect } from 'vitest';
import OV from '../js/lib/overall-view.js';

// /api/fleet/llama/aggregate
const LLAMA_AGG = {
  active_model_count: 0, active_models: [],
  agent_count_online: 2, agent_count_total: 2,
  agents: [
    { age_s: 2.0, agent_id: '056ba13c-689e-411e-b865-74a83b9086cd',
      model: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M', online: true,
      prompt_tokens_per_second: 0, state: 'sleeping', tokens_per_second: 0 },
    { age_s: 0.5, agent_id: '601964c9-eaeb-4c04-af3e-74e1b5808710',
      model: null, online: true,
      prompt_tokens_per_second: 0, state: 'sleeping', tokens_per_second: 0 },
  ],
  awake_agent_count: 0,
  gpu: { max_temp_c: 52.0, max_vram_pct: 19.0093994140625, total_power_watts: 20.0 },
  provider: 'llama',
  throughput: { total_pps: 0.0, total_tps: 0.0 },
};

// /api/fleet/lms/aggregate
const LMS_AGG = {
  agent_count_online: 3, agent_count_total: 3,
  agents: [
    { age_s: 2.1, agent_id: 'cc8ae083-738f-40b7-9b5b-18fffab4df98',
      busy_process_count: 0, loaded_model_count: 0, online: true, server_on: false },
    { age_s: 2.9, agent_id: '265ed10c-88c8-4a45-9127-0a6a397cc0c1',
      busy_process_count: 0, loaded_model_count: 0, online: true, server_on: false },
    { age_s: 3.1, agent_id: '42cf47d9-2849-47c3-99b9-bca2a9955fd1',
      busy_process_count: 0, loaded_model_count: 1, online: true, server_on: true },
  ],
  busy_agent_count: 0, busy_process_count_total: 0,
  loaded_model_count_total: 1, process_count_total: 1, provider: 'lms',
  server_on_count: 1,
};

// /api/fleet/vllm/aggregate
const VLLM_AGG = {
  active_model_count: 0, active_models: [],
  agent_count_online: 1, agent_count_total: 1,
  agents: [
    { age_s: 0.9, agent_id: 'dd90b4e9-d426-41e1-8951-ca55198146a2',
      model: null, online: true, requests_running: null,
      server_on: false, tokens_per_second: null },
  ],
  max_kv_cache_pct: 0.0, provider: 'vllm',
  requests_running_total: 0, requests_waiting_total: 0, server_on_count: 0,
  throughput: { total_pps: 0.0, total_tps: 0.0 },
  total_gpu_power_watts: 0.0,
};

// /api/agents/list-by-provider (trimmed)
const BY_PROVIDER = {
  llama: [
    { age_s: 3.0, agent_id: '601964c9-eaeb-4c04-af3e-74e1b5808710',
      hostname: 'llm-systems-agent-llama2', is_default: false, online: true },
    { age_s: 4.5, agent_id: '056ba13c-689e-411e-b865-74a83b9086cd',
      hostname: 'llm-systems-agent-llama', is_default: true, online: true },
  ],
  lms: [
    { age_s: 3.1, agent_id: '42cf47d9-2849-47c3-99b9-bca2a9955fd1',
      hostname: 'llm-systems-lmstudio.local', is_default: true, online: true },
    { age_s: 2.1, agent_id: 'cc8ae083-738f-40b7-9b5b-18fffab4df98',
      hostname: 'llm-systems-agent-lms', is_default: false, online: true },
    { age_s: 2.9, agent_id: '265ed10c-88c8-4a45-9127-0a6a397cc0c1',
      hostname: 'llm-systems-agent-lms2', is_default: false, online: true },
  ],
  vllm: [
    { age_s: 0.9, agent_id: 'dd90b4e9-d426-41e1-8951-ca55198146a2',
      hostname: 'llm-systems-agent-vllm', is_default: true, online: true },
  ],
};

// /api/history?since_minutes=1440&max_rows=180&fleet=all (two real rows;
// vllm fields absent on dev because its server is off — absence is the shape)
const HISTORY_ROWS = [
  { cpu_total: 2.598977, disk_root_pct: 49.9, gpu_power: 16.77, gpu_temp: 50.0,
    llama_pps: 4.25, llama_tps: 10.5, lms_pps: 0.5, lms_tps: 1.5,
    ram_percent: 89.0, ts: '2026-08-16T19:20:00+00:00' },
  { cpu_total: 3.384727, disk_root_pct: 49.9,
    ram_percent: 77.5, ts: '2026-08-16T19:10:00+00:00' },
];

// source-derived: models/alert.py to_dict() (no active alerts on dev)
const ALERTS = [
  { alert_id: 'a1', rule_name: 'GPU temp high', severity: 'critical',
    status: 'active', message: 'gpu_temp 87 > 85', source_host: 'agent-llama',
    created_at: '2026-08-17T10:00:00+00:00' },
  { alert_id: 'a2', rule_name: 'RAM warn', severity: 'warning',
    status: 'active', message: 'ram 91 > 90', source_host: 'agent-lms',
    created_at: '2026-08-17T11:00:00+00:00' },
  { alert_id: 'a3', rule_name: 'Disk info', severity: 'info',
    status: 'active', message: 'disk 60 > 50', source_host: 'agent-llama',
    created_at: '2026-08-17T09:00:00+00:00' },
];

// /api/energy/summary?days=1 (totals block trimmed to consumed fields)
const ENERGY = {
  ok: true,
  totals: { kwh: 1.448, cost_usd: 0.22, has_power: true, avg_watts: 31.3 },
};

describe('OV.heroSeries', () => {
  it('sums present provider fields null-safely', () => {
    const out = OV.heroSeries(HISTORY_ROWS);
    expect(out).toHaveLength(2);
    expect(out[0]).toMatchObject({ ts: '2026-08-16T19:20:00+00:00', gen: 12.0, prompt: 4.75 });
  });
  it('keeps gaps when no provider reported', () => {
    const out = OV.heroSeries(HISTORY_ROWS);
    expect(out[1].gen).toBeNull();
    expect(out[1].prompt).toBeNull();
  });
  it('tolerates empty/undefined rows', () => {
    expect(OV.heroSeries([])).toEqual([]);
    expect(OV.heroSeries(null)).toEqual([]);
  });
});

describe('OV.tiles', () => {
  it('returns off tiles with em-dash stats when all aggregates are null', () => {
    const tiles = OV.tiles(null, null, null);
    expect(tiles.map(t => t.key)).toEqual(['llama', 'lms', 'vllm']);
    expect(tiles.every(t => t.accent === 'off')).toBe(true);
    expect(tiles.every(t => t.online === 0 && t.total === 0)).toBe(true);
  });
  it('llama: online but none awake → warn; awake → ok; hot GPU → crit', () => {
    expect(OV.tiles(LLAMA_AGG, null, null)[0].accent).toBe('warn');
    const awake = { ...LLAMA_AGG, awake_agent_count: 1 };
    expect(OV.tiles(awake, null, null)[0].accent).toBe('ok');
    const hot = { ...LLAMA_AGG, gpu: { ...LLAMA_AGG.gpu, max_temp_c: 86 } };
    expect(OV.tiles(hot, null, null)[0].accent).toBe('crit');
  });
  it('lms: online without busy processes → warn; busy → ok', () => {
    expect(OV.tiles(null, LMS_AGG, null)[1].accent).toBe('warn');
    const busy = { ...LMS_AGG, busy_process_count_total: 2 };
    expect(OV.tiles(null, busy, null)[1].accent).toBe('ok');
  });
  it('vllm: requests running → ok', () => {
    expect(OV.tiles(null, null, VLLM_AGG)[2].accent).toBe('warn');
    const busy = { ...VLLM_AGG, requests_running_total: 3 };
    expect(OV.tiles(null, null, busy)[2].accent).toBe('ok');
  });
  it('carries online/total and string stats', () => {
    const t = OV.tiles(LLAMA_AGG, LMS_AGG, VLLM_AGG);
    expect(t[0].online).toBe(2);
    expect(t[0].total).toBe(2);
    expect(t[1].stats.every(s => typeof s.v === 'string' && typeof s.l === 'string')).toBe(true);
  });
});

describe('OV.agentRows', () => {
  it('joins hostnames and merges multi-provider agents into one row', () => {
    const rows = OV.agentRows([LLAMA_AGG, LMS_AGG, VLLM_AGG], BY_PROVIDER);
    expect(rows).toHaveLength(6);
    const llama1 = rows.find(r => r.hostname === 'llm-systems-agent-llama');
    expect(llama1.online).toBe(true);
    expect(llama1.provs).toHaveLength(1);
    expect(llama1.provs[0].prov).toBe('llama');
    expect(llama1.provs[0].detail).toContain('Qwen3.8-27B');
  });
  it('falls back to a shortened agent id when the hostname is unknown', () => {
    const rows = OV.agentRows([LLAMA_AGG, null, null], {});
    expect(rows[0].hostname).toMatch(/^[0-9a-f]{8}…$/);
  });
  it('sorts by hostname with offline agents last', () => {
    const off = JSON.parse(JSON.stringify(LLAMA_AGG));
    off.agents[1].online = false;
    const rows = OV.agentRows([off, null, null], BY_PROVIDER);
    expect(rows[rows.length - 1].online).toBe(false);
  });
});

describe('OV.alertsSummary', () => {
  it('empty → zero counts, null worst', () => {
    expect(OV.alertsSummary([])).toEqual(
      { total: 0, counts: { critical: 0, warning: 0, info: 0 }, worst: null, newest: [] });
  });
  it('counts by severity, worst=critical, newest 3 by created_at desc', () => {
    const s = OV.alertsSummary(ALERTS);
    expect(s.total).toBe(3);
    expect(s.counts).toEqual({ critical: 1, warning: 1, info: 1 });
    expect(s.worst).toBe('critical');
    expect(s.newest[0].rule).toBe('RAM warn');
    expect(s.newest[0].id).toBe('a2');
    expect(s.newest[0].severity).toBe('warning');
  });
  it('worst=warning when no critical; tolerates null input', () => {
    expect(OV.alertsSummary(ALERTS.slice(1)).worst).toBe('warning');
    expect(OV.alertsSummary(null).total).toBe(0);
  });
});

describe('OV.energyChip', () => {
  it('formats kwh + cost from a real summary', () => {
    const c = OV.energyChip(ENERGY);
    expect(c.kwh).toBe('1.4 kWh');
    expect(c.cost).toBe('$0.22');
  });
  it('null when summary missing, not ok, or unmetered', () => {
    expect(OV.energyChip(null)).toBeNull();
    expect(OV.energyChip({ ok: false })).toBeNull();
    expect(OV.energyChip({ ok: true, totals: { has_power: false } })).toBeNull();
  });
});

describe('OV.toplines', () => {
  it('returns 4 string stats from live aggregates', () => {
    const t = OV.toplines(LLAMA_AGG, LMS_AGG, VLLM_AGG, ENERGY);
    expect(t).toHaveLength(4);
    expect(t.every(s => typeof s.v === 'string' && typeof s.l === 'string')).toBe(true);
    expect(t[0].v).toBe('6');            // 2 + 3 + 1 agents online
    expect(t[3].v).toBe('1.4 kWh · $0.22');
  });
  it('em-dashes when everything is down', () => {
    const t = OV.toplines(null, null, null, null);
    expect(t[0].v).toBe('0');
    expect(t[2].v).toBe('—');
    expect(t[3].v).toBe('—');
  });
});

describe('OV.heroSeries with a bucket width (#576)', () => {
  const rows = [
    { ts: '2026-08-16T19:00:30+00:00', llama_tps: 2, llama_pps: 1 },
    { ts: '2026-08-16T19:03:00+00:00', llama_tps: 60, llama_pps: 20 },
    { ts: '2026-08-16T19:07:30+00:00', llama_tps: 5, llama_pps: 2 },
    { ts: '2026-08-16T19:09:00+00:00', llama_tps: 9, vllm_tps: 1, llama_pps: 3 },
    { ts: '2026-08-16T19:12:00+00:00' },
  ];
  it('keeps the peak provider-sum inside each bucket', () => {
    const out = OV.heroSeries(rows, 480000);
    expect(out).toHaveLength(3);
    expect(out[0].gen).toBe(60);
    expect(out[0].prompt).toBe(20);
    expect(out[1].gen).toBe(10);
    expect(out[1].prompt).toBe(3);
  });
  it('a bucket with no reporting rows stays null', () => {
    const out = OV.heroSeries(rows, 480000);
    expect(out[2].gen).toBeNull();
    expect(out[2].prompt).toBeNull();
  });
  it('bucket timestamps land on the bucket grid', () => {
    const out = OV.heroSeries(rows, 480000);
    expect(new Date(out[0].ts).getTime() % 480000).toBe(0);
    expect(new Date(out[1].ts).getTime()).toBeGreaterThan(new Date(out[0].ts).getTime());
  });
});

describe('OV.heroSeries junk timestamps (#576)', () => {
  it('skips rows whose ts cannot be bucketed', () => {
    const out = OV.heroSeries([
      { ts: 'not-a-date', llama_tps: 50 },
      { ts: '2026-08-16T19:03:00+00:00', llama_tps: 7 },
      { llama_tps: 9 },
    ], 480000);
    expect(out).toHaveLength(1);
    expect(out[0].gen).toBe(7);
  });
});

describe('OV.agentRows model names (#571)', () => {
  it('lms rows show loaded model names when available', () => {
    const rows = OV.agentRows([null, { agents: [{ agent_id: 'x', online: true,
      server_on: true, loaded_model_count: 2,
      loaded_models: ['qwen3-30b', 'llama-8b'] }] }, null], {});
    expect(rows[0].provs[0].detail).toBe('idle · qwen3-30b · llama-8b');
  });
  it('lms falls back to the count when names are absent', () => {
    const rows = OV.agentRows([null, { agents: [{ agent_id: 'x', online: true,
      server_on: true, loaded_model_count: 1 }] }, null], {});
    expect(rows[0].provs[0].detail).toBe('idle · 1 model');
  });
  it('vllm rows append the running model name', () => {
    const rows = OV.agentRows([null, null, { agents: [{ agent_id: 'y',
      online: true, server_on: true, requests_running: 2,
      model: 'meta-llama-3-8b' }] }], {});
    expect(rows[0].provs[0].detail).toBe('2 req · meta-llama-3-8b');
  });
  it('vllm rows without a model keep the request count only', () => {
    const rows = OV.agentRows([null, null, { agents: [{ agent_id: 'y',
      online: true, server_on: true, requests_running: 0 }] }], {});
    expect(rows[0].provs[0].detail).toBe('0 req');
  });
});

describe('OV.agentRows model-name caps (#571)', () => {
  it('mixed named/unnamed models surface the hidden count', () => {
    const rows = OV.agentRows([null, { agents: [{ agent_id: 'x', online: true,
      server_on: true, loaded_model_count: 2,
      loaded_models: ['qwen3-30b'] }] }, null], {});
    expect(rows[0].provs[0].detail).toBe('idle · qwen3-30b +1');
  });
  it('long lists cap at three names plus a remainder', () => {
    const rows = OV.agentRows([null, { agents: [{ agent_id: 'x', online: true,
      server_on: true, loaded_model_count: 5,
      loaded_models: ['a', 'b', 'c', 'd', 'e'] }] }, null], {});
    expect(rows[0].provs[0].detail).toBe('idle · a · b · c +2');
  });
});

describe('OV.agentRows lms state prefix (#571)', () => {
  it('busy agents lead with busy instead of idle', () => {
    const rows = OV.agentRows([null, { agents: [{ agent_id: 'x', online: true,
      server_on: true, busy_process_count: 1, loaded_model_count: 1,
      loaded_models: ['qwen3-30b'] }] }, null], {});
    expect(rows[0].provs[0].detail).toBe('busy · qwen3-30b');
  });
});

describe('OV.heroSeries power overlay (#577)', () => {
  const rows = [
    { ts: '2026-08-16T19:00:00+00:00', llama_tps: 1, psu_in: 220, gpu_power: 90 },
    { ts: '2026-08-16T19:03:00+00:00', llama_tps: 1, gpu_power: 120 },
    { ts: '2026-08-16T19:09:00+00:00', llama_tps: 1 },
  ];
  it('takes the larger of wall/gpu sums, keeps the bucket peak', () => {
    const out = OV.heroSeries(rows, 480000);
    expect(out[0].power).toBe(220);
    expect(out[1].power).toBeNull();
    expect(OV.heroSeries([
      { ts: '2026-08-16T19:00:00+00:00', psu_in: 100, gpu_power: 300 },
    ])[0].power).toBe(300);
  });
  it('unbucketed rows carry power too', () => {
    expect(OV.heroSeries(rows)[1].power).toBe(120);
  });
});

describe('OV.energySeries (#577)', () => {
  it('maps chart labels onto their hourly Wh bucket', () => {
    const hourly = [
      { hour_ts: 360000, energy_wh: 50 },
      { hour_ts: 363600, energy_wh: 20 },
    ];
    const labels = [360000000, 360480000, 363660000, 367200000];
    expect(OV.energySeries(hourly, labels)).toEqual([50, 50, 20, null]);
  });
  it('degrades to empty on junk input', () => {
    expect(OV.energySeries(null, [1])).toEqual([null]);
    expect(OV.energySeries([], [])).toEqual([]);
  });
});

describe('OV.tiles unified layout + peaks (#591)', () => {
  const NOW = Date.UTC(2026, 7, 21, 18, 0, 0);
  const PEAKS = {
    llama: { gen: { v: 51.3, t: NOW - 3 * 60000 }, prompt: { v: 452.5, t: NOW - 3 * 60000 } },
    lms: { gen: null, prompt: null },
    vllm: { gen: { v: 12, t: NOW - 90000 }, prompt: null },
  };

  it('every tile leads with gen and prompt t/s', () => {
    for (const t of OV.tiles(LLAMA_AGG, LMS_AGG, VLLM_AGG, PEAKS, NOW)) {
      expect(t.stats[0].l).toBe('gen t/s');
      expect(t.stats[1].l).toBe('prompt t/s');
      expect(t.stats).toHaveLength(4);
    }
  });

  it('renders peak sub-lines with value and age', () => {
    const [llama, lms, vllm] = OV.tiles(LLAMA_AGG, LMS_AGG, VLLM_AGG, PEAKS, NOW);
    expect(llama.stats[0].p).toBe('peak 51.3 · 3m ago');
    expect(llama.stats[1].p).toBe('peak 452.5 · 3m ago');
    expect(vllm.stats[0].p).toBe('peak 12.0 · 1m ago');
    expect(lms.stats[0].p).toBeNull();
    expect(vllm.stats[1].p).toBeNull();
  });

  it('provider-specific stats carry no peak field', () => {
    const [llama, lms, vllm] = OV.tiles(LLAMA_AGG, LMS_AGG, VLLM_AGG, PEAKS, NOW);
    expect(llama.stats[2].p).toBeUndefined();
    expect(lms.stats[2].l).toBe('servers on');
    expect(lms.stats[3].l).toBe('models loaded');
    expect(vllm.stats[2].l).toBe('requests');
    expect(vllm.stats[3].l).toBe('kv cache');
  });

  it('missing peaks argument keeps tiles renderable', () => {
    const [llama] = OV.tiles(LLAMA_AGG, null, null);
    expect(llama.stats[0].p).toBeNull();
  });

  it('lms tile reads its gateway throughput block', () => {
    const withTp = { ...LMS_AGG, throughput: { total_tps: 7.5, total_pps: 30.2 } };
    const [, lms] = OV.tiles(null, withTp, null, {}, NOW);
    expect(lms.stats[0].v).toBe('7.5');
    expect(lms.stats[1].v).toBe('30.2');
  });
});
