// #565: pure view-model transforms for the Overall page (agent rows, alerts).
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

describe('OV.agentRows model names (#571)', () => {
  it('lms rows show loaded model names when available', () => {
    const rows = OV.agentRows([null, { agents: [{ agent_id: 'x', online: true,
      server_on: true, loaded_model_count: 2,
      loaded_models: ['qwen3-30b', 'llama-8b'] }] }, null], {});
    expect(rows[0].provs[0].detail).toBe('idle · qwen3-30b · llama-8b');
  });
  it('lms rows say the ps read failed instead of showing 0 models', () => {
    const rows = OV.agentRows([null, { agents: [{ agent_id: 'x', online: true,
      server_on: true, loaded_model_count: 0, loaded_models: [],
      ps_error: 'lms ps timed out after 15s' }] }, null], {});
    expect(rows[0].provs[0].detail).toBe('lms ps unreadable');
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

describe('OV agent-row model extras (#593)', () => {
  it('fmtShort scales k/M/B and rejects junk', () => {
    expect(OV.fmtShort(1234567)).toBe('1.2M');
    expect(OV.fmtShort(89012)).toBe('89.0k');
    expect(OV.fmtShort(512)).toBe('512');
    expect(OV.fmtShort(3200000000)).toBe('3.2B');
    expect(OV.fmtShort(null)).toBeNull();
    expect(OV.fmtShort(NaN)).toBeNull();
  });

  it('llama rows append ctx and totals after the model name', () => {
    const aggs = [{ agents: [{ agent_id: 'x', online: true, state: 'awake',
      model: 'qwen3-30b', ctx: 32768,
      total_tokens_generated: 1234567, total_tokens_prompted: 89012 }] }, null, null];
    const [row] = OV.agentRows(aggs, {});
    expect(row.provs[0].detail)
      .toBe('awake · qwen3-30b · 32.8k ctx · 89.0k prompt · 1.2M gen');
  });

  it('extras are omitted piecewise when fields are absent (old agents)', () => {
    const aggs = [{ agents: [{ agent_id: 'x', online: true, state: 'awake',
      model: 'm', total_tokens_generated: 500 }] }, null, null];
    const [row] = OV.agentRows(aggs, {});
    expect(row.provs[0].detail).toBe('awake · m · 500 gen');
  });

  it('no extras without a loaded model', () => {
    const aggs = [{ agents: [{ agent_id: 'x', online: true, state: 'sleeping',
      model: null, ctx: 32768 }] }, null, null];
    const [row] = OV.agentRows(aggs, {});
    expect(row.provs[0].detail).toBe('sleeping');
  });

  it('vllm rows append ctx from max_model_len mapping', () => {
    const aggs = [null, null, { agents: [{ agent_id: 'y', online: true,
      server_on: true, model: 'nemotron', requests_running: 2,
      ctx: 131072, total_tokens_generated: 555 }] }];
    const [row] = OV.agentRows(aggs, {});
    expect(row.provs[0].detail).toBe('2 req · nemotron · 131.1k ctx · 555 gen');
  });

  it('lms rows append extras after the model list', () => {
    const aggs = [null, { agents: [{ agent_id: 'z', online: true, server_on: true,
      busy_process_count: 0, loaded_model_count: 1, loaded_models: ['big'],
      ctx: 32768, total_tokens_generated: 900, total_tokens_prompted: 300 }] }, null];
    const [row] = OV.agentRows(aggs, {});
    expect(row.provs[0].detail).toBe('idle · big · 32.8k ctx · 300 prompt · 900 gen');
  });
});
