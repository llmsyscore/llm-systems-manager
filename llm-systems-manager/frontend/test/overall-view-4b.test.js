// #1432 phase 4b: Overall-on-the-kit view transforms in js/lib/overall-view.js.
// Fixtures cut from the dev payloads of 2026-11-16 (trimmed to the fields read).
import { describe, it, expect } from 'vitest';
import OV from '../js/lib/overall-view.js';

const ID_LLAMA = '056ba13c-689e-411e-b865-74a83b9086cd';
const ID_LLAMA2 = '601964c9-eaeb-4c04-af3e-74e1b5808710';
const ID_LMS = '42cf47d9-2849-47c3-99b9-bca2a9955fd1';
const ID_LMS2 = 'cc8ae083-738f-40b7-9b5b-18fffab4df98';
const ID_VLLM = 'dd90b4e9-d426-41e1-8951-ca55198146a2';

// /api/fleet/<p>/aggregate
const LLAMA_AGG = {
  active_model_count: 0, agent_count_online: 2, agent_count_total: 2, awake_agent_count: 1,
  agents: [
    { age_s: 2.2, agent_id: ID_LLAMA2, model: null, online: true, power_watts: null, state: 'awake' },
    { age_s: 3.1, agent_id: ID_LLAMA, model: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M', online: true,
      power_watts: 11.0, state: 'sleeping' },
  ],
  gpu: { max_temp_c: 47.0, thermal_crit_count: 0, total_power_watts: 11.0 },
  throughput: { total_pps: 0.0, total_tps: 0.0 },
};
const LMS_AGG = {
  agent_count_online: 2, agent_count_total: 2, loaded_model_count_total: 1, server_on_count: 1,
  busy_process_count_total: 0,
  agents: [
    { age_s: 0.8, agent_id: ID_LMS, busy_process_count: 0, loaded_model_count: 1,
      loaded_models: ['qwen3.5-9b@q6_k'], online: true, power_watts: 0.502, server_on: true },
    { age_s: 3.1, agent_id: ID_LMS2, busy_process_count: 0, loaded_model_count: 0,
      loaded_models: [], online: true, power_watts: null, ps_error: 'LMS_CMD not found', server_on: false },
  ],
  gpu: { max_temp_c: 0.0, thermal_crit_count: 0, total_power_watts: 0.502 },
  throughput: { total_pps: 0.0, total_tps: 0.0 },
};
const VLLM_AGG = {
  active_model_count: 0, active_models: [], agent_count_online: 1, agent_count_total: 1,
  agents: [{ age_s: 2.8, agent_id: ID_VLLM, model: null, online: true, power_watts: null,
             requests_running: null, server_on: false }],
  gpu: { max_temp_c: 0.0, thermal_crit_count: 0, total_power_watts: 0.0 },
  max_kv_cache_pct: 0.0, requests_running_total: 0, requests_waiting_total: 0, server_on_count: 0,
  throughput: { total_pps: 0.0, total_tps: 0.0 }, total_gpu_power_watts: 0.0,
};
const AGGS = { llama: LLAMA_AGG, lms: LMS_AGG, vllm: VLLM_AGG };

// /api/agents/list-by-provider (hostnames as in the energy summary)
const BY_PROVIDER = {
  llama: [{ agent_id: ID_LLAMA, hostname: 'llm-systems-agent-llama' },
          { agent_id: ID_LLAMA2, hostname: 'llm-systems-agent-llama2' }],
  lms: [{ agent_id: ID_LMS, hostname: 'llm-systems-lmstudio.local' },
        { agent_id: ID_LMS2, hostname: 'llm-systems-agent-lms' }],
  vllm: [{ agent_id: ID_VLLM, hostname: 'llm-systems-agent-vllm1' }],
};

// /api/energy/summary?days=1
const SUMMARY1 = {
  config: { cloud_price_label: 'Opus 5', price_kwh: 0.15 }, price_kwh: 0.15, savings_usd: 3.34, ok: true,
  hosts: [
    { active_kwh: 0.02, agent_id: ID_LLAMA, cloud_cost_usd: 0.71, cost_usd: 0.04, has_power: true,
      hostname: 'llm-systems-agent-llama', idle_kwh: 0.233, kwh: 0.253, power_source: 'gpu', tokens_gen: 10237 },
    { active_kwh: 0.021, agent_id: ID_LMS, cloud_cost_usd: 2.67, cost_usd: 0.01, has_power: true,
      hostname: 'llm-systems-lmstudio.local', idle_kwh: 0.015, kwh: 0.036, power_source: 'mac', tokens_gen: 55007 },
    { active_kwh: null, agent_id: ID_LLAMA2, cloud_cost_usd: 0.0, cost_usd: null, has_power: false,
      hostname: 'llm-systems-agent-llama2', idle_kwh: null, kwh: null, power_source: null, tokens_gen: 0 },
    { active_kwh: null, agent_id: ID_VLLM, cloud_cost_usd: 0.0, cost_usd: null, has_power: false,
      hostname: 'llm-systems-agent-vllm1', idle_kwh: null, kwh: null, power_source: null, tokens_gen: 0 },
    { active_kwh: null, agent_id: ID_LMS2, cloud_cost_usd: 0.0, cost_usd: null, has_power: false,
      hostname: 'llm-systems-agent-lms', idle_kwh: null, kwh: null, power_source: null, tokens_gen: 0 },
  ],
  models: [
    { hosts: ['llm-systems-agent-llama'], kwh: 0.253, model: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M' },
    { hosts: ['llm-systems-lmstudio.local'], kwh: 0.036, model: 'qwen3.5-9b@q6_k' },
    { hosts: ['llm-systems-agent-llama2', 'llm-systems-agent-vllm1', 'llm-systems-agent-lms',
              'llm-systems-lmstudio.local'], kwh: 0.0, model: '(no model)' },
  ],
  totals: { active_kwh: 0.041, cost_usd: 0.04, has_power: true, idle_kwh: 0.247, kwh: 0.288,
            tokens_gen: 65244, usd_per_mtok: 0.6629 },
};

// /api/energy/hourly rows (the last four hours of the 168 h sample)
const END_TS = 1794812400;
const HOURLY_ROWS = [
  { active_energy_wh: 2.35, energy_wh: 13.21, hour_ts: 1794798000, tokens_gen: 633 },
  { active_energy_wh: 3.82, energy_wh: 17.4, hour_ts: 1794801600, tokens_gen: 6504 },
  { active_energy_wh: 1.16, energy_wh: 12.52, hour_ts: 1794805200, tokens_gen: 2647 },
  { active_energy_wh: 0.0, energy_wh: 4.7, hour_ts: 1794808800, tokens_gen: 0 },
];
// hours=48 shape: the first two rows moved a day back as "the 24 h before"
const HOURLY48 = { end_ts: END_TS, rows: [
  { ...HOURLY_ROWS[1], hour_ts: END_TS - 86400 - 3600 },
  { ...HOURLY_ROWS[0], hour_ts: END_TS - 86400 - 7200 },
  { ...HOURLY_ROWS[2], hour_ts: END_TS - 7200 },
  { ...HOURLY_ROWS[3], hour_ts: END_TS - 3600 },
] };

// /api/history?fleet=all rows (real rows; the 10:34 row is added for the peak check)
const NOW = Date.parse('2026-11-16T06:25:00+00:00');
const HIST = [
  { ts: '2026-11-15T06:20:00+00:00', cpu_total: 4.1 },
  { ts: '2026-11-15T10:32:00+00:00', gpu_power: 9.0, llama_pps: 0.0, llama_tps: 0.0, lms_pps: 979.49, lms_tps: 29.46 },
  { ts: '2026-11-15T10:34:00+00:00', gpu_power: 12.0, llama_pps: 5.0, llama_tps: 0.0, lms_pps: 1000, lms_tps: 12 },
  { ts: '2026-11-15T11:06:00+00:00', gpu_power: 9.0, llama_pps: 0.0, llama_tps: 0.0, lms_pps: 772.17, lms_tps: 196.5 },
  { ts: '2026-11-15T13:02:00+00:00', gpu_power: 35.275, llama_pps: 616.522, llama_tps: 94.1566, lms_pps: 0.0, lms_tps: 0.0 },
  { ts: 'not-a-date', llama_tps: 999 },
];

// /api/llama-state
const LLAMA_STATE = {
  agent_online: true, aggregate: 'sleeping', model: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M', state: 'sleeping',
  power: { applied: 'powersave', awake: 'performance', sleep: 'powersave', enabled: true,
    counters: { failures: 0, switches: 21 },
    last_switch: { outcome: 'verified', ts: 1794803524.591955 },
    readback: [{ actual: 'low', expected: 'low', label: 'gpu level', ok: true },
               { actual: '2', expected: '2', label: 'gpu profile', ok: true }] },
  residency: { aggregate: 'sleeping', models: [
    { model_id: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M', provider: 'llama', status: 'sleeping' },
    { model_id: 'unsloth/Qwen3.8-27B-GGUF:Q4_K_XL', provider: 'llama', status: 'unloaded' }] },
};
// /api/llm/models: dev returned data: []; the args are source-derived from the mockup presets
const LLM_MODELS = { data: [{ id: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M', status: { value: 'sleeping',
  args: ['llama-server', '--ctx-size', '124160', '--parallel', '1', '--cache-type-k', 'q8_0',
         '--spec-type', 'draft-mtp', '--reasoning', 'on'] } }] };

// /api/lmstudio/metrics
const LMS_METRICS = {
  agent_online: true, gateway_inflight: 0, gateway_tokens: { gen: 8639, prompt: 27654 },
  mac_power: { ane_w: 0.0, cpu_package_w: 0.502, ecore_util_pct: 52.5, gpu_busy_pct: 0.0, gpu_w: 0.0,
               pcore_freq_mhz: 2837.6, pcore_util_pct: 8.2, soc_total_w: 0.502, thermal_pressure: 'Nominal' },
  models: ['qwen3.5-9b@q6_k', 'qwen3.5-4b', 'nvidia/nvidia-nemotron-3-nano-4b', 'gemma-4-e4b-it', 'qwen3-8b',
           'qwen3.5-9b@q4_k_m', 'carnice-9b-sft-claude-opus-reasoning-unsloth', 'prism-ml/bonsai-27b',
           'nvidia/nemotron-3-nano-4b', 'text-embedding-nomic-embed-text-v1.5'].map(id => ({ id, object: 'model' })),
  ps: [{ context: 32768, identifier: 'qwen3.5-9b@q6_k', model: 'qwen3.5-9b@q6_k', parallel: 1, params: '9B',
         quant: 'Q6_K', size: '9.28 GB', status: 'IDLE' }],
  server: { on: true, port: 1235 },
  system: { cpu_name: 'Apple M2 Pro', host: 'llm-systems-lmstudio.local',
            ram: { percent: 82.1, total_bytes: 17179869184 }, swap: { percent: 90.0, total_bytes: 6442450944 } },
};

// /api/forecast
const FORECAST = {
  enabled: true, last_result: '10 found, 0 new', last_run: 1794729648.5106082, next_run: 1794816007.0718868,
  window_days: 14, tower: { model: 'qwen3.5-9b@q6_k' },
  checks: ['disk_fill', 'load_shift', 'memory_headroom', 'thermal_trend', 'power_cost', 'throughput',
           'slot_pressure', 'model_churn', 'alarm_patterns', 'agent_health', 'service_health',
           'bench_outcomes', 'capacity', 'idle_waste'].map(id => ({ id, state: 'ok' }))
    .concat([{ id: 'model_errors', state: 'collecting' }, { id: 'weekly_digest', state: 'off' }]),
  findings: [{ check: 'disk_fill', confidence: 'medium',
    detail: 'Disk use on / has grown steadily and reaches 100 % around Nov 27. Likely cause: Unbounded log retention or cache growth in the agent service',
    first_seen: 1793257201.5412338, host: 'llm-systems-agent-opc-herm', last_seen: 1794729608.0672548,
    severity: 'warning', status: 'open', summary: '/ full in 13 days (+0.3 GB/day)', title: 'Disk fill' }],
  cleared: [
    { check: 'load_shift', detail: 'A sustained change, not a spike. Likely cause: Sudden spike in concurrent inference requests or memory leak during model loading',
      host: 'llm-systems-agent-lms', resolved: 1794384008.0262027, severity: 'warning', status: 'cleared', title: 'Load shift' },
    { check: 'weekly_digest', detail: 'llm-systems-agent-opc-herm: / full in 9 days (+0.4 GB/day)',
      host: null, resolved: 1794413324.9081435, severity: 'info', status: 'dismissed', title: 'Weekly digest' },
    { check: 'service_health', detail: 'The slowest responses of the day take much longer than they did.',
      host: 'llm-systems-manager-dev', resolved: 1794099718.4512267, severity: 'warning', status: 'dismissed', title: 'Service health' },
  ],
};

// /api/tower/state and /api/tower/insights
const TOWER_STATE = {
  check: { grade: 'native' }, diagnose_alarms: true, enabled: true, fallback_enabled: true,
  fallback: { hosts: ['llm-systems-agent-llama'], model: 'bartowski/Qwen3.8-27B-GGUF:Q4_K_M' },
  hosts: ['llm-systems-lmstudio.local'], model: 'qwen3.5-9b@q6_k', timers: 0,
};
const TOWER_INSIGHTS = { insights: [], new: 0, ok: true };

// source-derived: models/alert.py to_dict() (source_host is the agent hostname)
const ALERTS = [
  { alert_id: 'a1', rule_name: 'GPU temp high', severity: 'critical', message: 'gpu_temp 87 > 85',
    source_host: 'llm-systems-agent-llama', created_at: '2026-11-16T05:00:00+00:00' },
  { alert_id: 'a2', rule_name: 'RAM warn', severity: 'warning', message: 'ram 91 > 90',
    source_host: 'llm-systems-agent-lms', created_at: '2026-11-16T06:00:00+00:00' },
  { alert_id: 'a3', rule_name: 'Disk info', severity: 'info', message: 'disk 60 > 50',
    source_host: null, created_at: '2026-11-16T04:00:00+00:00' },
];

const LMS_IDS = OV.hostIds(BY_PROVIDER, 'lms');
const localHHMM = ms => {
  const d = new Date(ms);
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
};

describe('4b: range, buckets and labels', () => {
  it('rangeBucketMs follows the range; invalid input falls back to 24 h', () => {
    expect(OV.rangeBucketMs(6)).toBe(60000);
    expect(OV.rangeBucketMs(12)).toBe(300000);
    expect(OV.rangeBucketMs(24)).toBe(300000);
    expect(OV.rangeBucketMs(7)).toBe(300000);
    expect(OV.rangeBucketMs(null)).toBe(300000);
    expect(OV.rangeBucketMs('6')).toBe(60000);
  });
  it('bucketLabel gives local HH:MM and empty text for junk', () => {
    expect(OV.bucketLabel(NOW)).toBe(localHHMM(NOW));
    expect(OV.bucketLabel(NOW)).toMatch(/^\d\d:\d\d$/);
    expect(OV.bucketLabel(NaN)).toBe('');
    expect(OV.bucketLabel(null)).toBe('');
  });
  it('hostIds(lms) holds the LM Studio agent ids; all gives null', () => {
    expect(LMS_IDS.has(ID_LMS)).toBe(true);
    expect(LMS_IDS.has(ID_LMS2)).toBe(true);
    expect(LMS_IDS.has(ID_LLAMA)).toBe(false);
    expect(LMS_IDS.has(ID_VLLM)).toBe(false);
    expect([...LMS_IDS].filter(x => x.includes('-') && x.length === 36).sort()).toEqual([ID_LMS, ID_LMS2].sort());
    expect(OV.hostIds(BY_PROVIDER, 'all')).toBeNull();
    expect(OV.hostIds(null, 'vllm').size).toBe(0);
  });
});

describe('4b: clock change (America/New_York, 2026-11-01)', () => {
  // Runs with the zone switched for the test body only; labels must stay unique in the repeated hour.
  const inNY = fn => {
    const prev = process.env.TZ;
    process.env.TZ = 'America/New_York';
    try { fn(); } finally { if (prev === undefined) delete process.env.TZ; else process.env.TZ = prev; }
  };
  it('throughput labels stay unique across the repeated hour', () => {
    inNY(() => {
      expect(new Date(Date.parse('2026-11-01T05:30:00Z')).getHours()).toBe(1);
      expect(new Date(Date.parse('2026-11-01T06:30:00Z')).getHours()).toBe(1);
      for (const [h, now] of [[24, '2026-11-01T12:00:00Z'], [12, '2026-11-01T09:00:00Z'], [6, '2026-11-01T07:00:00Z']]) {
        const t = OV.throughput([], h, 'all', Date.parse(now));
        expect(new Set(t.labels).size).toBe(t.keep);
        expect(t.labels.some(l => l.endsWith('′'))).toBe(true);
      }
      const t24 = OV.throughput([], 24, 'all', Date.parse('2026-11-01T12:00:00Z'));
      expect(t24.labels.filter(l => l.startsWith('01:30'))).toEqual(['01:30', '01:30′']);
    });
  });
  it('no prime when the window starts after the first copy of the repeated time', () => {
    inNY(() => {
      const late = OV.throughput([], 6, 'all', Date.parse('2026-11-01T11:59:00Z'));
      expect(late.labels[0]).toBe('01:00');
      expect(late.labels.some(l => l.endsWith('′'))).toBe(false);
      // window 01:45 EDT .. 06:44 EST in 5 min slots: 01:00-01:40 EST are first copies here, 01:45 EST repeats
      const mid = OV.throughput([], 6, 'all', Date.parse('2026-11-01T11:44:00Z'));
      expect(mid.labels[0]).toBe('01:45');
      expect(mid.labels).toContain('01:00');
      expect(mid.labels).not.toContain('01:00′');
      expect(mid.labels).toContain('01:45′');
      expect(new Set(mid.labels).size).toBe(72);
      expect(OV.rangeLabel(Date.parse('2026-11-01T06:45:00Z'), 6)).toBe(OV.bucketLabels(Date.parse('2026-11-01T00:50:00Z'), 72, 300000)[71]);
    });
  });
  it('zero-filled gap buckets take their labels from the window ending at the current bucket', () => {
    inNY(() => {
      const first = Date.parse('2026-11-01T05:15:00Z');
      const a = OV.liveBucket(null, first, 24, {}, 'all');
      const b = OV.liveBucket(a.state, Date.parse('2026-11-01T06:40:00Z'), 24, {}, 'all');
      const labels = b.append.map(x => x.label).concat([b.update.label]);
      expect(labels[0]).toBe('01:15');
      expect(labels).toContain('01:30');
      expect(labels).toContain('01:00′');
      expect(b.update.label).toBe('01:30′');
      expect(new Set(labels).size).toBe(labels.length);
    });
  });
  it('liveBucket labels a bucket the same way throughput does', () => {
    inNY(() => {
      const second = Date.parse('2026-11-01T06:30:00Z');
      const t = OV.throughput([], 24, 'all', second);
      const live = OV.liveBucket(null, second + 1000, 24, {}, 'all');
      expect(live.update.label).toBe('01:30′');
      expect(t.labels[t.labels.length - 1]).toBe(live.update.label);
      const next = OV.liveBucket(live.state, second + 900000, 24, {}, 'all');
      expect(next.append[0].label).toBe('01:30′');
      expect(OV.rangeLabel(Date.parse('2026-11-01T05:30:00Z'), 24)).toBe('01:30');
    });
  });
});

describe('4b: throughput', () => {
  it('keeps 96/72/72 bar slots of 15/10/5 minutes with unique labels ending at the current slot', () => {
    const t24 = OV.throughput(HIST, 24, 'all', NOW);
    expect(t24.keep).toBe(96);
    expect(t24.rangeH).toBe(24);
    expect(new Set(t24.labels).size).toBe(96);
    expect(t24.labels[95]).toBe(localHHMM(Math.floor(NOW / 900000) * 900000));
    expect(OV.throughput(HIST, 12, 'all', NOW).keep).toBe(72);
    const t6 = OV.throughput(HIST, 6, 'all', NOW);
    expect(t6.keep).toBe(72);
    expect(t6.labels[71]).toBe(localHHMM(Math.floor(NOW / 300000) * 300000));
    expect([6, 12, 24].map(OV.tpBucketMs)).toEqual([300000, 600000, 900000]);
  });
  it('Prompt (all) first, then one Generation-band series per provider, with slot peaks and means; empty slots 0', () => {
    const t = OV.throughput(HIST, 24, 'all', NOW);
    expect(t.series.map(s => s.name)).toEqual(['Prompt (all)', 'llama.cpp', 'LM Studio', 'vLLM']);
    expect(t.series.map(s => s.band)).toEqual([undefined, 'Generation', 'Generation', 'Generation']);
    const start = Math.floor(NOW / 900000) * 900000 - 95 * 900000;
    const at = iso => Math.floor((Date.parse(iso) - start) / 900000);
    const i1030 = at('2026-11-15T10:30:00Z'), i1100 = at('2026-11-15T11:00:00Z'), i1300 = at('2026-11-15T13:00:00Z');
    expect(t.series[2].values[i1030]).toBe(29.46);
    expect(t.series[2].avg[i1030]).toBeCloseTo(20.73, 5);
    expect(t.series[0].values[i1030]).toBe(1005);
    expect(t.series[0].avg[i1030]).toBeCloseTo(992.245, 5);
    expect(t.series[2].values[i1100]).toBe(196.5);
    expect(t.series[1].values[i1300]).toBe(94.1566);
    expect(t.series[3].values.every(v => v === 0)).toBe(true);
    expect(t.series[1].values[0]).toBe(0);
    expect(t.series[1].avg[0]).toBe(0);
    expect(t.power[i1030]).toBe(12);
    expect(t.series.every(s => s.values.length === 96 && s.avg.length === 96)).toBe(true);
  });
  it('drops rows outside the range and junk timestamps', () => {
    const t = OV.throughput(HIST, 6, 'all', NOW);
    expect(t.series.every(s => s.values.every(v => v === 0))).toBe(true);
    const sum = OV.throughput(HIST, 24, 'all', NOW).series[1].values.reduce((a, b) => a + b, 0);
    expect(sum).toBe(94.1566);
  });
  it('fleet power per row is the larger of psu_in and gpu_power when psu_in is reported', () => {
    const rows = [{ ts: '2026-11-16T06:00:00Z', gpu_power: 30, psu_in: 120 }, { ts: '2026-11-16T06:05:00Z', gpu_power: 50, psu_in: 20 },
      { ts: '2026-11-16T06:10:00Z', gpu_power: 9 }];
    const t = OV.throughput(rows, 6, 'all', Date.parse('2026-11-16T06:12:00Z'));
    expect(t.power.slice(-3)).toEqual([120, 50, 9]);
  });
  it('tpLegend: prompt tokens first, then tokens per provider over 24 h, a host under two providers counting under each', () => {
    const s1 = { hosts: [
      { agent_id: ID_LLAMA, tokens_gen: 10237, tokens_prompt: 91227 },
      { agent_id: ID_LMS, tokens_gen: 55007, tokens_prompt: 250000 },
      { agent_id: ID_LMS2, tokens_gen: 0, tokens_prompt: 8782 }] };
    const by = { ...BY_PROVIDER, vllm: [{ agent_id: ID_LMS, hostname: 'llm-systems-lmstudio.local' }] };
    expect(OV.tpLegend(s1, by, 'all')).toEqual(['350.0k tokens', '10.2k tokens', '55.0k tokens', '55.0k tokens']);
    expect(OV.tpLegend(s1, by, 'lms')).toEqual(['258.8k tokens', '55.0k tokens']);
    expect(OV.tpLegend(s1, { llama: [], lms: [], vllm: [] }, 'vllm')).toEqual(['0 tokens', '0 tokens']);
    expect(OV.tpLegend(null, by, 'all')).toEqual(['–', '–', '–', '–']);
  });
  it('a provider filter keeps that provider and its prompt rate only, named after it', () => {
    const t = OV.throughput(HIST, 24, 'lms', NOW);
    expect(t.series.map(s => s.name)).toEqual(['Prompt (LM Studio)', 'LM Studio']);
    expect(['all', 'llama', 'lms', 'vllm', 'x'].map(OV.promptName))
      .toEqual(['Prompt (all)', 'Prompt (llama.cpp)', 'Prompt (LM Studio)', 'Prompt (vLLM)', 'Prompt (all)']);
    const start = Math.floor(NOW / 900000) * 900000 - 95 * 900000;
    expect(t.series[0].values[Math.floor((Date.parse('2026-11-15T10:30:00Z') - start) / 900000)]).toBe(1000);
  });
});

describe('4b: liveBucket', () => {
  const tick = (tps, pps) => ({ llama: { throughput: { total_tps: tps, total_pps: pps } }, lms: LMS_AGG, vllm: VLLM_AGG });
  const S15 = 900000, B0 = Math.floor(NOW / S15) * S15;
  it('folds ticks in one slot into the peak and the mean without appending', () => {
    const a = OV.liveBucket(null, B0 + 1000, 24, tick(40, 100), 'all');
    expect(a.append).toBeNull();
    expect(a.update).toEqual({ label: localHHMM(B0), values: [100, 40, 0, 0], avgs: [100, 40, 0, 0] });
    const b = OV.liveBucket(a.state, B0 + 60000, 24, tick(20, 300), 'all');
    expect(b.append).toBeNull();
    expect(b.update.values).toEqual([300, 40, 0, 0]);
    expect(b.update.avgs).toEqual([200, 30, 0, 0]);
  });
  it('a tick in the next slot appends the finished slot with its mean', () => {
    const a = OV.liveBucket(null, B0 + 1000, 24, tick(40, 100), 'all');
    const a2 = OV.liveBucket(a.state, B0 + 2000, 24, tick(0, 100), 'all');
    const b = OV.liveBucket(a2.state, B0 + S15, 24, tick(5, 7), 'all');
    expect(b.append).toEqual([{ label: localHHMM(B0), values: [100, 40, 0, 0], avgs: [100, 20, 0, 0] }]);
    expect(b.update).toEqual({ label: localHHMM(B0 + S15), values: [7, 5, 0, 0], avgs: [7, 5, 0, 0] });
    expect(b.state.key).toBe(B0 + S15);
  });
  it('a state from another provider filter restarts the slot', () => {
    const a = OV.liveBucket(null, B0 + 1000, 24, tick(40, 100), 'all');
    const b = OV.liveBucket(a.state, B0 + S15, 24, tick(40, 100), 'llama');
    expect(b.append).toBeNull();
    expect(b.update.values).toEqual([100, 40]);
  });
  it('a 3-slot gap appends the finished slot and zero-fills the skipped ones', () => {
    const a = OV.liveBucket(null, B0 + 1000, 24, tick(40, 100), 'all');
    const b = OV.liveBucket(a.state, B0 + 4 * S15 + 10, 24, tick(5, 7), 'all');
    const z = { values: [0, 0, 0, 0], avgs: [0, 0, 0, 0] };
    expect(b.append).toEqual([
      { label: localHHMM(B0), values: [100, 40, 0, 0], avgs: [100, 40, 0, 0] },
      { label: localHHMM(B0 + S15), ...z }, { label: localHHMM(B0 + 2 * S15), ...z }, { label: localHHMM(B0 + 3 * S15), ...z },
    ]);
    expect(b.update.label).toBe(localHHMM(B0 + 4 * S15));
    expect(b.gap).toBe(3);
    expect(OV.liveBucket(b.state, B0 + 9 * S15, 24, tick(1, 1), 'all').gap).toBe(4);
    expect(OV.liveBucket(b.state, B0 + 4 * S15 + 20, 24, tick(1, 1), 'all').gap).toBe(0);
  });
  it('a gap longer than the range appends at most keep slots', () => {
    const a = OV.liveBucket(null, B0, 6, tick(1, 1), 'all');
    const b = OV.liveBucket(a.state, B0 + 3 * 86400000, 6, tick(1, 1), 'all');
    expect(b.append.length).toBeLessThanOrEqual(73);
    expect(b.append[b.append.length - 1].label).toBe(localHHMM(B0 + 3 * 86400000 - 300000));
  });
});

describe('4b: fleetKpis', () => {
  const tp = OV.throughput(HIST, 24, 'all', NOW);
  const base = { aggs: AGGS, summary1: SUMMARY1, hourly48: HOURLY48, tp, ids: null };
  it('six tiles in order with the fleet values', () => {
    const k = OV.fleetKpis(base);
    expect(k.map(x => x.id)).toEqual(['k-agents', 'k-tok', 'k-peak', 'k-pow', 'k-en', 'k-save']);
    const by = Object.fromEntries(k.map(x => [x.id, x]));
    expect(by['k-agents']).toMatchObject({ value: 5, unit: '/ 5', sub: 'all reporting in under 4 s', delta: null, spark: null });
    expect(by['k-agents'].title).toBe('Hosts online');
    expect(by['k-tok']).toMatchObject({ title: 'Tokens', value: '65.2k', sub: 'last 24 h · 7.1k the day before', spark: [2647, 0] });
    expect(by['k-tok'].delta).toEqual({ dir: 'up', text: '814%', tone: 'good' });
    expect(by['k-peak']).toMatchObject({ value: '196.5', unit: 't/s', sub: 'highest combined rate in a 15 min slot · last 24 h', delta: null });
    expect(by['k-peak'].spark).toHaveLength(96);
    expect(by['k-pow']).toMatchObject({ value: '11.5', unit: 'W', delta: null });
    expect(by['k-pow'].spark).toHaveLength(96);
    // 6 and 12 hours both keep 72 slots; the range comes from tp.rangeH.
    const k12 = OV.fleetKpis({ ...base, tp: OV.throughput(HIST, 12, 'all', NOW) }).find(x => x.id === 'k-peak');
    expect(k12.sub).toBe('highest combined rate in a 10 min slot · last 12 h');
    expect(by['k-en']).toMatchObject({ value: '0.29', unit: 'kWh', sub: '$0.04 at $0.15/kWh', spark: [12.52, 4.7] });
    expect(by['k-en'].delta).toEqual({ dir: 'up', text: '841%', tone: 'bad' });
    expect(by['k-save']).toMatchObject({ value: '$3.34', sub: 'same tokens at Opus 5 prices', delta: null, spark: null });
  });
  it('falling tokens are bad and falling energy good', () => {
    const big = { end_ts: END_TS, rows: [{ hour_ts: END_TS - 90000, tokens_gen: 100000, energy_wh: 1000 }] };
    const k = OV.fleetKpis({ ...base, hourly48: big });
    expect(k[1].delta).toEqual({ dir: 'down', text: '35%', tone: 'bad' });
    expect(k[4].delta).toEqual({ dir: 'down', text: '71%', tone: 'good' });
  });
  it('recomputes for the LM Studio hosts', () => {
    const by = Object.fromEntries(OV.fleetKpis({ ...base, ids: LMS_IDS }).map(x => [x.id, x]));
    expect(by['k-agents']).toMatchObject({ value: 2, unit: '/ 2' });
    expect(by['k-tok'].value).toBe('55.0k');
    expect(by['k-pow'].value).toBe('0.5');
    expect(by['k-en'].value).toBe('0.04');
    expect(by['k-save'].value).toBe('$2.66');
    expect(by['k-pow'].spark).toBeNull();
  });
  it('an empty id array narrows to no hosts, null does not narrow', () => {
    expect(OV.fleetKpis({ ...base, ids: [] })[0]).toMatchObject({ value: 0, unit: '/ 0' });
    expect(OV.fleetKpis({ ...base, ids: null })[0]).toMatchObject({ value: 5, unit: '/ 5' });
    expect(OV.energyByHost(SUMMARY1, []).rows).toEqual([]);
    expect(OV.hostsByTokens(SUMMARY1, [])).toEqual([]);
  });
  it('a provider with no hosts shows 0 and dashes', () => {
    const by = Object.fromEntries(OV.fleetKpis({ ...base, ids: new Set() }).map(x => [x.id, x]));
    expect(by['k-agents']).toMatchObject({ value: 0, unit: '/ 0', sub: 'No hosts yet' });
    expect(by['k-tok'].value).toBeNull();
    expect(by['k-pow'].value).toBeNull();
    expect(by['k-en']).toMatchObject({ value: null, sub: 'No power readings yet', delta: null });
    expect(by['k-save'].value).toBeNull();
  });
});

describe('4b: serverPanels', () => {
  const CAPS = { llama_present: true, lms_present: true, vllm_present: true };
  const input = { llamaState: LLAMA_STATE, llmModels: LLM_MODELS, lms: LMS_METRICS, aggs: AGGS, caps: CAPS };
  it('llama.cpp: state, model, preset args, power policy and models on the server', () => {
    const p = OV.serverPanels(input).llama;
    expect(p.state).toEqual({ label: 'Sleeping', tone: 'sl', detail: 'wakes on the next request' });
    expect(p.note).toBe('1 of 2 awake');
    expect(p.kv).toEqual([
      ['Generation', '0.0 t/s'], ['Prompt', '0.0 t/s'],
      ['Model', 'Qwen3.8-27B-GGUF:Q4_K_M'],
      ['Context', '124.2k tokens · 1 slot · KV q8_0'],
      ['Extras', 'speculative decoding (draft-mtp) · reasoning on'],
      ['Loaded models', '0'],
      ['GPU power', 'Power mode changed 21 times, 0 failed · last verified'],
    ]);
    expect(p.pills.map(x => x.label)).toEqual(['Power saving now', 'Performance when awake', 'gpu level low ✓', 'gpu profile 2 ✓']);
    expect(p.pills[0].tone).toBe('sl');
    expect(p.models.map(m => [m.label, m.status, m.sub])).toEqual([
      ['bartowski/Qwen3.8-27B:Q4_K_M', 'Sleeping', '124.2k ctx'],
      ['unsloth/Qwen3.8-27B:Q4_K_XL', 'Not loaded', ''],
    ]);
    expect(p.table.cols).toEqual(['Field', 'Value']);
    expect(p.table.rows[0]).toEqual(['State', 'Sleeping']);
    expect(p.table.rows).toContainEqual(['Hosts awake', '1 of 2 awake']);
    expect(p.hot).toBe(false);
  });
  it('llama.cpp shows an unknown aggregate as it is', () => {
    const st = { ...LLAMA_STATE, aggregate: 'draining', residency: { ...LLAMA_STATE.residency, aggregate: 'draining' } };
    expect(OV.serverPanels({ ...input, llamaState: st }).llama.state).toEqual({ label: 'Draining', tone: 'off', detail: '' });
    expect(OV.serverPanels({ ...input, llamaState: {} }).llama.state.label).toBe('Nothing reported yet');
    const proto = OV.serverPanels({ ...input, llamaState: { ...LLAMA_STATE, aggregate: 'constructor' } }).llama;
    expect(proto.state).toEqual({ label: 'Constructor', tone: 'off', detail: '' });
  });
  it('LM Studio with an error or a failed ps read is not reachable, error in the hint', () => {
    const err = OV.serverPanels({ ...input, lms: { ...LMS_METRICS, error: 'agent timed out' } }).lms;
    expect(err.state).toEqual({ label: 'Not reachable', tone: 'bad', detail: '' });
    expect(err.hint).toBe("LM Studio didn't answer.");
    const ps = OV.serverPanels({ ...input, lms: { ...LMS_METRICS, ps_ok: false, ps_error: 'LMS_CMD not found' } }).lms;
    expect(ps.state.label).toBe('Not reachable');
    expect(ps.hint).toBe("LM Studio's command-line tool isn't on this host.");
    expect(OV.serverPanels({ ...input, lms: { ...LMS_METRICS, ps_ok: false } }).lms.hint).toBe("LM Studio didn't answer.");
    expect(OV.serverPanels(input).lms.hint).toBeNull();
  });
  it('llama.cpp without preset args leaves the context lines out', () => {
    const p = OV.serverPanels({ ...input, llmModels: { data: [] } }).llama;
    expect(p.kv.map(r => r[0])).toEqual(['Generation', 'Prompt', 'Model', 'Loaded models', 'GPU power']);
  });
  it('LM Studio: server, loaded model, installed, gateway tokens, memory, Mac power', () => {
    const p = OV.serverPanels(input).lms;
    expect(p.state).toEqual({ label: 'Server on', tone: 'ok', detail: 'port 1235' });
    expect(p.note).toBe('llm-systems-lmstudio · 2 of 2 hosts online');
    expect(p.kv).toEqual([
      ['Generation', '0.0 t/s'], ['Prompt', '0.0 t/s'],
      ['Loaded', 'qwen3.5-9b@q6_k · Idle'],
      ['Model', '9B · Q6_K · 9.28 GB · 32.8k ctx'],
      ['Installed', '10 models'],
      ['Through manager', '8.6k out · 27.7k in · 0 running'],
      ['Memory', '82% of 16 GB · swap 90% of 6.0 GB'],
      ['Loaded models', '1'],
    ]);
    expect(p.swapHigh).toBe(false);
    expect(p.power.title).toBe('Apple M2 Pro power now · thermal Nominal');
    expect(p.power.split).toEqual([{ label: 'CPU', v: 0.502 }, { label: 'GPU', v: 0 }, { label: 'Neural Engine', v: 0 }]);
    expect(p.power.sub).toBe('Performance cores 8% at 2.84 GHz · efficiency cores 53% · GPU busy 0%');
  });
  it('swap above 90 % sets swapHigh', () => {
    const hot = { ...LMS_METRICS, system: { ...LMS_METRICS.system, swap: { percent: 96.3, total_bytes: 21474836480 } } };
    expect(OV.serverPanels({ ...input, lms: hot }).lms.swapHigh).toBe(true);
    const table = OV.serverPanels(input).lms.table.rows;
    expect(table).toContainEqual(['Mac power (W)', '0.502']);
  });
  it('vLLM: server off with requests, cache, power and heartbeat', () => {
    const p = OV.serverPanels(input).vllm;
    expect(p.state.label).toBe('Server off');
    expect(p.kv).toEqual([
      ['Generation', '0.0 t/s'], ['Prompt', '0.0 t/s'],
      ['Model', 'none served'], ['Requests', '0 running · 0 waiting'], ['KV cache', '0% full'],
      ['GPU power', '0 W'], ['Last report', '2.8 s ago'], ['Loaded models', '0'],
    ]);
    expect(p.hint).toBe('Start the server from LLM Control to see its requests and cache here.');
  });
  it('live speed with rolling peaks, GPU hot and the llama.cpp detail host', () => {
    const now = Date.parse('2026-11-16T06:00:00Z');
    const lms = { ...LMS_AGG, gpu: { ...LMS_AGG.gpu, thermal_crit_count: 1 },
      throughput: { total_tps: 12.34, total_pps: 40.06, window: {
        gen: { peak: { ts: '2026-11-16T05:48:00+00:00', v: 197.2 } },
        prompt: { peak: { ts: '2026-11-16T03:00:00+00:00', v: 899.78 } } } } };
    const p = OV.serverPanels({ ...input, aggs: { ...AGGS, lms }, now, llamaHost: 'llm-systems-agent-llama.local' });
    expect(p.lms.kv.slice(0, 2)).toEqual([['Generation', '12.3 t/s · peak 197 t/s · 12 min ago'], ['Prompt', '40.1 t/s · peak 900 t/s · 3 h ago']]);
    expect(p.lms.table.rows.slice(0, 4)).toEqual([['State', 'Server on'], ['GPU temperature', 'Hot'],
      ['Generation', '12.3 t/s · peak 197 t/s · 12 min ago'], ['Prompt', '40.1 t/s · peak 900 t/s · 3 h ago']]);
    expect(p.lms.hot).toBe(true);
    expect(p.llama.hot).toBe(false);
    expect(p.llama.note).toBe('llm-systems-agent-llama · 1 of 2 awake');
    const noTp = { ...VLLM_AGG };
    delete noTp.throughput;
    expect(OV.serverPanels({ ...input, aggs: { ...AGGS, vllm: noTp } }).vllm.kv[0][0]).toBe('Model');
  });
  it('a provider switched off or without agents gets no panel', () => {
    const none = { ...VLLM_AGG, agent_count_total: 0, agent_count_online: 0, agents: [] };
    expect(OV.serverPanels({ ...input, aggs: { ...AGGS, vllm: none } }).vllm).toBeNull();
    const off = OV.serverPanels({ ...input, caps: { lms_present: false, vllm_present: false } });
    expect(off.lms).toBeNull();
    expect(off.vllm).toBeNull();
    expect(off.llama).not.toBeNull();
    const absent = OV.serverPanels({ ...input, caps: {} });
    expect(absent.llama).not.toBeNull();
    expect(absent.lms).not.toBeNull();
    expect(absent.vllm).toBeNull();
    expect(OV.serverPanels({ ...input, caps: null }).vllm).toBeNull();
    expect(OV.serverPanels({ ...input, caps: { llama_present: false } }).llama).toBeNull();
  });
});

describe('4b: energy and power panels', () => {
  it('energyPerHour with a step sums hours into local steps that start at a multiple of the step', () => {
    const mid = new Date(2026, 10, 16, 0, 0, 0).getTime() / 1000;
    const rows = [0, 1, 2, 3, 5].map(h => ({ hour_ts: mid + h * 3600, energy_wh: 10, active_energy_wh: h }));
    const e = OV.energyPerHour(rows, 3);
    expect(e.labels).toEqual(['Mon 00:00', 'Mon 03:00']);
    expect(e.series[1].values).toEqual([3, 8]);
    expect(e.series[0].values).toEqual([27, 12]);
    expect(e.totals.kwh).toBe(0.05);
    expect(OV.energyPerHour(rows, 1).labels).toHaveLength(5);
    expect(OV.energyPerHour(rows, 9).labels).toHaveLength(5);
    expect(OV.energyPerHour(rows, 12).labels).toEqual(['Nov 16 00:00']);
    expect(OV.energyPerHour(rows, 24).series[1].values).toEqual([11]);
  });
  it('energyPerHour at step 12 labels steps with the date and keeps a leading partial step', () => {
    const start = new Date(2026, 9, 20, 5, 0, 0).getTime() / 1000;
    const rows = Array.from({ length: 672 }, (_, i) => ({ hour_ts: start + i * 3600, energy_wh: 10, active_energy_wh: 4 }));
    const e = OV.energyPerHour(rows, 12);
    expect(e.labels[0]).toBe('Oct 20 00:00');
    expect(new Set(e.labels).size).toBe(e.labels.length);
    const sum = a => a.reduce((x, y) => x + y, 0);
    expect(Math.round(sum(e.series[1].values))).toBe(672 * 4);
    expect(Math.round(sum(e.series[0].values))).toBe(672 * 6);
    expect(OV.energyPerHour(rows, 3).labels[0]).toBe('Oct 20 03:00');
    expect(OV.energyPerHour(rows.slice(0, 100), 3).labels[0]).toMatch(/^[A-Z][a-z]{2} \d\d:00$/);
  });
  it('energyPerHour with a step leaves out a first step a week before the last one with the same label, keeping its energy in the totals', () => {
    const now = new Date(2026, 10, 17, 10, 0, 0).getTime() / 1000;
    const rows = [];
    for (let h = 167; h >= 0; h--) rows.push({ hour_ts: now - h * 3600, energy_wh: 1, active_energy_wh: 0 });
    const e = OV.energyPerHour(rows, 3, { wrap: true });
    expect(e.labels[0]).toBe('Tue 12:00');
    expect(OV.energyPerHour(rows, 3).labels[0]).toBe('Tue 09:00');
    expect(e.labels[e.labels.length - 1]).toBe('Tue 09:00');
    expect(new Set(e.labels).size).toBe(e.labels.length);
    expect(e.totals.kwh).toBe(0.168);
    // Data that starts mid-step without a week of rows keeps its first bar.
    const short = rows.slice(-5);
    expect(OV.energyPerHour(short, 3, { wrap: true }).labels).toEqual(['Tue 06:00', 'Tue 09:00']);
  });
  it('energyPerHour with a step groups a fall-back DST day by local hour', () => {
    const tz = process.env.TZ;
    process.env.TZ = 'America/New_York';
    try {
      // 2026-11-01: 01:00 happens twice in New York.
      const start = Date.UTC(2026, 10, 1, 4) / 1000;
      const rows = [0, 1, 2, 3, 4].map(h => ({ hour_ts: start + h * 3600, energy_wh: 10, active_energy_wh: 0 }));
      const e = OV.energyPerHour(rows, 3);
      expect(e.labels).toEqual(['Sun 00:00', 'Sun 03:00']);
      expect(e.series[0].values).toEqual([40, 10]);
    } finally {
      if (tz === undefined) delete process.env.TZ; else process.env.TZ = tz;
    }
  });
  it('energyPerHour: Idle and Generating Wh, summed kWh, local labels', () => {
    const e = OV.energyPerHour({ end_ts: END_TS, rows: HOURLY_ROWS.slice().reverse() });
    expect(e.series.map(s => s.name)).toEqual(['Idle', 'Generating']);
    expect(e.series[0].values).toEqual([10.86, 13.58, 11.36, 4.7]);
    expect(e.series[1].values).toEqual([2.35, 3.82, 1.16, 0]);
    expect(e.totals).toEqual({ kwh: 0.048, active_kwh: 0.007, idle_kwh: 0.041 });
    const d = new Date(1794798000 * 1000);
    expect(e.labels[0]).toBe(['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'][d.getDay()] + ' '
      + String(d.getHours()).padStart(2, '0') + ':00');
    expect(OV.energyPerHour(HOURLY_ROWS).labels).toHaveLength(4);
  });
  it('energyByHost ranks metered hosts in Wh and counts hosts without power', () => {
    const e = OV.energyByHost(SUMMARY1, null);
    expect(e.rows).toEqual([
      { label: 'llm-systems-agent-llama', v: 253, val: ID_LLAMA },
      { label: 'llm-systems-lmstudio', v: 36, val: ID_LMS },
    ]);
    expect(e.noPower).toBe(3);
    expect(e.sub).toBe('3 hosts report no power. Same tokens at Opus 5 prices: $3.34 saved · $0.66 per million tokens here.');
    const l = OV.energyByHost(SUMMARY1, LMS_IDS);
    expect(l.rows.map(r => r.val)).toEqual([ID_LMS]);
    expect(l.noPower).toBe(1);
    expect(l.sub).toBe('1 host reports no power. Same tokens at Opus 5 prices: $2.66 saved.');
  });
  it('energySplit: generating vs idle and energy per model', () => {
    expect(OV.energySplit(SUMMARY1, null)).toEqual({
      split: [{ label: 'Generating', v: 41 }, { label: 'Idle', v: 247 }],
      models: [{ label: 'Qwen3.8-27B-GGUF:Q4_K_M', v: 253 }, { label: 'qwen3.5-9b@q6_k', v: 36 }],
      unit: 'Wh',
    });
    const l = OV.energySplit(SUMMARY1, LMS_IDS);
    expect(l.split).toEqual([{ label: 'Generating', v: 21 }, { label: 'Idle', v: 15 }]);
    expect(l.models).toEqual([]);
  });
  it('powerNow: one row per powered host, total and hosts without a sensor', () => {
    const p = OV.powerNow(AGGS, BY_PROVIDER, null);
    expect(p.rows.map(r => [r.label, r.v, r.sub])).toEqual([
      ['llm-systems-agent-llama', 11, 'GPU'], ['llm-systems-lmstudio', 0.5, 'Mac power']]);
    expect(p.total).toBe(11.5);
    expect(p.noSensor).toBe(3);
    expect(OV.powerNow(AGGS, BY_PROVIDER, LMS_IDS)).toMatchObject({ total: 0.5, noSensor: 1 });
  });
  it('powerNow counts a host serving two providers once', () => {
    const dual = { agents: [{ agent_id: ID_LLAMA, online: true, power_watts: 11.0 }] };
    const p = OV.powerNow([LLAMA_AGG, dual, null], BY_PROVIDER, null);
    expect(p.rows).toHaveLength(1);
    expect(p.total).toBe(11);
    expect(p.rows[0].sub).toBe('GPU');
  });
  it('hostsByTokens ranks hosts by tokens generated', () => {
    const r = OV.hostsByTokens(SUMMARY1, null);
    expect(r).toHaveLength(5);
    expect(r[0]).toEqual({ label: 'llm-systems-lmstudio', v: 55007, val: ID_LMS });
    expect(r[1].v).toBe(10237);
    expect(OV.hostsByTokens(SUMMARY1, LMS_IDS).map(x => x.val)).toEqual([ID_LMS, ID_LMS2]);
  });
  it('hostRows narrows agentRows to the chosen hosts', () => {
    expect(OV.hostRows(AGGS, BY_PROVIDER, null)).toHaveLength(5);
    expect(OV.hostRows(AGGS, BY_PROVIDER, LMS_IDS).map(r => r.hostname).sort())
      .toEqual(['llm-systems-agent-lms', 'llm-systems-lmstudio.local']);
  });
});

describe('4b: watch list feeds', () => {
  it('forecastFeed: open findings, recently cleared, note and checks line', () => {
    const f = OV.forecastFeed(FORECAST, null);
    expect(f.open).toEqual([{ kind: 'Warning',
      title: 'Disk use on / has grown steadily and reaches 100 % around Nov 27.',
      detail: 'llm-systems-agent-opc-herm · Disk fill · medium confidence', at: 1794729608.0672548 * 1000 }]);
    expect(f.cleared.map(x => [x.kind, x.title])).toEqual([
      ['Dismissed', 'Weekly digest'], ['Cleared', 'Load shift on llm-systems-agent-lms'],
      ['Dismissed', 'Service health on llm-systems-manager-dev']]);
    expect(f.cleared[1].detail).toBe('A sustained change, not a spike.');
    expect(f.note).toBe('10 found, 0 new');
    expect(f.sub).toBe('14 checks on 14 days of data · written by qwen3.5-9b@q6_k');
    expect(f.nextAt).toBe(1794816007.0718868 * 1000);
  });
  it('forecastFeed sorts open findings by severity then newest, cleared by time with unreadable last', () => {
    const f = OV.forecastFeed({ findings: [
      { severity: 'info', detail: 'i new.', last_seen: 300 },
      { severity: 'warning', detail: 'w old.', last_seen: 100 },
      { severity: 'critical', detail: 'c.', last_seen: 50 },
      { severity: 'warning', detail: 'w new.', last_seen: '2026-11-16T06:00:00Z' },
      { severity: 'odd', detail: 'line one\nline two', last_seen: 999 },
    ], cleared: [
      { check: 'a', resolved: 'junk' }, { check: 'b', resolved: 1794384008 }, { check: 'c', resolved: '2026-11-16T05:00:00Z' },
    ] }, null);
    expect(f.open.map(x => x.title)).toEqual(['c.', 'w new.', 'w old.', 'i new.', 'line one']);
    expect(f.cleared.map(x => x.title)).toEqual(['C', 'B', 'A']);
  });
  it('forecastFeed narrows to the chosen hosts', () => {
    const f = OV.forecastFeed(FORECAST, LMS_IDS);
    expect(f.open).toEqual([]);
    expect(f.cleared.map(x => x.title)).toEqual(['Load shift on llm-systems-agent-lms']);
    expect(OV.forecastFeed({ enabled: false }, null).note).toBe('Forecast is off');
  });
  it('towerFeed: no insight yet, model, fallback and activity', () => {
    const t = OV.towerFeed(TOWER_STATE, TOWER_INSIGHTS);
    expect(t.insight).toBeNull();
    expect(t.kv).toEqual([
      ['Model', 'qwen3.5-9b@q6_k on llm-systems-lmstudio · tools native'],
      ['Fallback', 'Qwen3.8-27B-GGUF:Q4_K_M on llm-systems-agent-llama'],
      ['Activity', '0 timers · alarm diagnosis on'],
    ]);
    expect(t.note).toBe('0 insights');
  });
  it('towerFeed: the newest insight with its action and checks', () => {
    // source-derived: the mockup's Tower insight fields
    const ins = { insights: [{ severity: 'warning', summary: 'RAM keeps climbing', host: 'llm-systems-agent-lms',
      rule: 'RAM warn', status: 'open', created: 1794800000, suggested_action: 'Restart the agent.',
      checks: [{ name: 'host_metrics', ok: true, summary: 'read' }] }] };
    const t = OV.towerFeed(TOWER_STATE, ins);
    expect(t.insight).toEqual({ kind: 'Warning', title: 'RAM keeps climbing',
      detail: 'llm-systems-agent-lms · RAM warn · open', at: 1794800000000, action: 'Restart the agent.',
      checks: [{ label: 'host metrics', ok: true, title: 'read' }] });
    expect(t.note).toBe('1 insight');
    expect(OV.towerFeed({ enabled: false }, null).kv).toEqual([['Tower', 'Off']]);
  });
  it('alertsFeed: newest first, narrowed by source host, with alertsSummary', () => {
    const a = OV.alertsFeed(ALERTS, null);
    expect(a.rows.map(r => r.title)).toEqual(['RAM warn', 'GPU temp high', 'Disk info']);
    expect(a.rows[0]).toEqual({ kind: 'Warning', title: 'RAM warn', detail: 'ram 91 > 90 · llm-systems-agent-lms',
      at: Date.parse('2026-11-16T06:00:00+00:00') });
    expect(a.summary.total).toBe(3);
    const l = OV.alertsFeed(ALERTS, LMS_IDS);
    expect(l.rows.map(r => r.title)).toEqual(['RAM warn']);
    expect(l.summary).toMatchObject({ total: 1, worst: 'warning' });
  });
  it('alertsFeed sorts by time across UTC offsets, unreadable times last', () => {
    const rows = OV.alertsFeed([
      { rule_name: 'early', created_at: '2026-11-16T07:00:00+02:00' },
      { rule_name: 'junk', created_at: 'not-a-date' },
      { rule_name: 'late', created_at: '2026-11-16T06:00:00+00:00' },
      { rule_name: 'mid', created_at: '2026-11-16T05:30:00Z' },
    ], null).rows.map(r => r.title);
    expect(rows).toEqual(['late', 'mid', 'early', 'junk']);
  });
});

describe('4b: mapLegacyBand', () => {
  it('maps hero and tiles to rows, drops the rest and the old keys once', () => {
    const layout = { overallBandHidden: ['hero', 'agents', 'tiles'], overallBandOrder: ['tiles', 'hero'], theme: 'dark' };
    expect(OV.mapLegacyBand(layout)).toEqual({ changed: true });
    expect(layout).toEqual({ overallRowsHidden: ['throughput', 'servers'], theme: 'dark' });
    expect(OV.mapLegacyBand(layout)).toEqual({ changed: false });
  });
  it('merges into saved rows without duplicates and drops an order alone', () => {
    const layout = { overallBandHidden: ['hero'], overallRowsHidden: ['throughput', 'power'] };
    OV.mapLegacyBand(layout);
    expect(layout.overallRowsHidden).toEqual(['throughput', 'power']);
    const order = { overallBandOrder: ['hero'] };
    expect(OV.mapLegacyBand(order)).toEqual({ changed: true });
    expect(order).toEqual({});
    expect(OV.mapLegacyBand(null)).toEqual({ changed: false });
  });
  it('alerts, forecast and agents hidden together hide the Watch list row; fewer do not', () => {
    const all3 = { overallBandHidden: ['agents', 'alerts', 'forecast', 'constructor'] };
    OV.mapLegacyBand(all3);
    expect(all3).toEqual({ overallRowsHidden: ['watch'] });
    const two = { overallBandHidden: ['alerts', 'forecast'] };
    OV.mapLegacyBand(two);
    expect(two).toEqual({});
  });
});

// Every string and number reachable from a value (Sets included).
function leaves(v, out = []) {
  if (typeof v === 'string' || typeof v === 'number') out.push(v);
  else if (v instanceof Set) v.forEach(x => leaves(x, out));
  else if (v && typeof v === 'object') Object.values(v).forEach(x => leaves(x, out));
  return out;
}

describe('4b: empty input', () => {
  it('all transforms survive empty input', () => {
    const fns = {
      rangeBucketMs: 1, bucketLabel: 1, hostIds: 2, throughput: 4, liveBucket: 5, fleetKpis: 1,
      serverPanels: 1, energyPerHour: 1, energyByHost: 2, energySplit: 2, powerNow: 3, hostsByTokens: 2,
      hostRows: 3, forecastFeed: 2, towerFeed: 2, alertsFeed: 2, mapLegacyBand: 1, rangeLabel: 2, bucketLabels: 3,
      tpLegend: 3,
    };
    const empties = [null, {}, [], Infinity];
    let calls = 0;
    for (const [name, n] of Object.entries(fns)) {
      for (const e of empties) {
        const args = [];
        for (let i = 0; i < n; i++) args.push(e && typeof e === 'object' ? (Array.isArray(e) ? [] : {}) : e);
        const out = OV[name](...args);
        calls++;
        for (const x of leaves(out)) {
          if (typeof x === 'number') expect(Number.isFinite(x), `${name}(${JSON.stringify(e)}) number`).toBe(true);
          else expect(x, `${name}(${JSON.stringify(e)})`).not.toMatch(/NaN|undefined|null|Infinity/);
        }
      }
    }
    expect(calls).toBe(80);
    for (const name of ['fleetKpis', 'serverPanels']) {
      for (const input of [{ aggs: null, summary1: null, hourly48: null, tp: null, ids: null },
                           { llamaState: null, llmModels: null, lms: null, aggs: null, caps: null }]) {
        for (const x of leaves(OV[name](input))) {
          if (typeof x === 'string') expect(x).not.toMatch(/NaN|undefined|null|Infinity/);
          else expect(Number.isFinite(x)).toBe(true);
        }
      }
    }
  });
});
