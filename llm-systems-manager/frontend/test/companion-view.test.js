import { describe, it, expect } from 'vitest';
import CView from '../js/lib/companion-view.js';

const NOW = 1_700_000_000; // fixed epoch seconds for deterministic ages

describe('CView.age', () => {
  it('formats buckets', () => {
    expect(CView.age(NOW - 10, NOW)).toBe('just now');
    expect(CView.age(NOW - 120, NOW)).toBe('2m ago');
    expect(CView.age(NOW - 3 * 3600, NOW)).toBe('3h ago');
    expect(CView.age(NOW - 2 * 86400, NOW)).toBe('2d ago');
  });
  it('accepts ISO strings and epoch ms', () => {
    const iso = new Date(NOW * 1000 - 120000).toISOString();
    expect(CView.age(iso, NOW)).toBe('2m ago');
    expect(CView.age((NOW - 120) * 1000, NOW)).toBe('2m ago');
  });
  it('returns dash for junk', () => {
    expect(CView.age(null, NOW)).toBe('—');
    expect(CView.age('not a date', NOW)).toBe('—');
  });
});

describe('CView.glance', () => {
  const full = {
    metrics: {
      gpu: { temperature_c: 62, vram_used_mb: 21914, vram_usage_percent: 89, power_watts: 300 },
      llama: { model: 'qwen3-32b-q4_k_m', tokens_per_second: 42.7, n_tokens_max: 32768,
               active_slots: 2, total_slots: 4 },
      liquidctl: { psu: { 'Estimated input power': { value: 412 } } },
    },
    llama: { state: 'awake', model: 'qwen3-32b-q4_k_m' },
    lms: { ps: [{ model: 'llama-3.3-70b', status: 'loaded' }], system: { host: 'mac-studio' },
           mac_power: { gpu_busy_pct: 71 }, gateway_rates: { gen_tps: 12 } },
    vllm: { vllm: { state: 'stopped' } },
    energy: { price_kwh: 0.20, totals: { cost_usd: 1.84, kwh: 9.2 } },
  };

  it('hero picks the fastest live provider (and exposes its rate for the strip)', () => {
    const g = CView.glance(full);
    expect(g.hero.n).toBe('42.7');
    expect(g.hero.tps).toBeCloseTo(42.7);
    expect(g.hero.unit).toBe('tok/s');
    expect(g.hero.label).toBe('llama.cpp · qwen3-32b-q4_k_m');
  });

  it('vLLM running shows its inference status + model', () => {
    const g = CView.glance({ vllm: { vllm: { state: 'running', model: 'mixtral',
      requests_running: 3, tokens_per_second: 55 } } });
    expect(g.providers[2]).toMatchObject({ status: 'ok', detail: 'generating · mixtral' });
    expect(g.hero.label).toBe('vLLM · mixtral');
  });

  it('all three provider rows read the same two things: status + GPU use', () => {
    const g = CView.glance({ ...full,
      metrics: { ...full.metrics, gpu: { ...full.metrics.gpu, gpu_util_percent: 96 } },
      vllm: { vllm: { state: 'running', model: 'mixtral' },
              system: { gpu: { gpu_util_percent: 12 } } } });
    expect(g.providers.map((p) => p.rUnit)).toEqual(['gpu', 'gpu', 'gpu']);
    expect(g.providers[0]).toMatchObject({ status: 'ok', name: 'llama.cpp',
      detail: 'generating · qwen3-32b-q4_k_m', rN: '96%' });
    expect(g.providers[1]).toMatchObject({ status: 'ok', name: 'LM Studio',
      detail: 'generating · llama-3.3-70b', rN: '71%' });
    expect(g.providers[2]).toMatchObject({ status: 'ok', name: 'vLLM',
      detail: 'idle · mixtral', rN: '12%' });
  });

  it('a provider with no GPU telemetry dashes its GPU cell, keeps its status', () => {
    const g = CView.glance(full);
    expect(g.providers[2]).toMatchObject({ status: 'idle', name: 'vLLM',
      detail: 'stopped', rN: '—', rUnit: 'gpu' });
  });

  it('LMS: an IDLE ps row is loaded-not-generating, not absent', () => {
    // Regression: real `lms ps` reports loaded-but-not-generating as "IDLE".
    const g = CView.glance({ lms: { ps: [{ model: 'nvidia/nemotron-3-nano-4b', status: 'IDLE' }],
      system: { host: 'mac-studio' }, mac_power: { gpu_busy_pct: 4 } } });
    expect(g.providers[1]).toMatchObject({ status: 'ok',
      detail: 'idle · nvidia/nemotron-3-nano-4b', rN: '4%' });
  });

  it('system tiles: temp, VRAM with total + hot flag, PSU power, energy today', () => {
    const g = CView.glance(full);
    expect(g.tiles[0]).toMatchObject({ v: '62', unit: '°C', meter: 62, hot: false });
    expect(g.tiles[1]).toMatchObject({ v: '21.4', unit: '/ 24 GB', meter: 89, hot: true });
    expect(g.tiles[2]).toMatchObject({ v: '412', unit: 'W', sub: 'PSU · 1 host' });
    expect(g.tiles[3]).toMatchObject({ v: '$1.84', sub: '9.2 kWh · $0.2/kWh' });
  });

  it('input power sums every reporting host, deduped, not just llama.cpp', () => {
    const g = CView.glance({ ...full,
      lms: { ...full.lms, mac_power: { gpu_busy_pct: 71, soc_total_w: 38 } } });
    expect(g.tiles[2]).toMatchObject({ v: '450', unit: 'W', sub: 'PSU + SoC · 2 hosts' });
  });

  it('input power falls back to the energy accumulator when no sample has watts', () => {
    const g = CView.glance({ energy: { totals: { avg_watts: 118.4 } } });
    expect(g.tiles[2]).toMatchObject({ v: '118', sub: 'fleet avg · this window' });
  });

  it('degrades to dashes/idle on an empty payload without throwing', () => {
    const g = CView.glance({});
    expect(g.hero.n).toBe('0.0');
    expect(g.hero.label).toContain('idle');
    expect(g.providers.every((p) => p.status === 'idle')).toBe(true);
    expect(g.providers.every((p) => p.rN === '—')).toBe(true);
    expect(g.tiles[0].v).toBe('—');
    expect(g.tiles[2]).toMatchObject({ v: '—', sub: 'no telemetry' });
    expect(g.tiles[3].sub).toBe('no telemetry');
  });

  it('flags a hot GPU at the warn threshold', () => {
    const g = CView.glance({ metrics: { gpu: { temperature_c: 87 } } });
    expect(g.tiles[0].hot).toBe(true);
  });
});

describe('CView.alerts', () => {
  const list = [
    { alert_id: 'a1', severity: 'critical', status: 'active', message: 'GPU temperature 91 °C ≥ 90 °C',
      metric_source: 'system', metric_name: 'gpu_temp', source_host: 'llm-core', created_at: (NOW - 120) * 1000 },
    { alert_id: 'a2', severity: 'warning', status: 'acknowledged', message: 'VRAM 95% for 10 min',
      metric_source: 'system', metric_name: 'vram_used_pct', source_host: 'llm-core', created_at: (NOW - 840) * 1000 },
    { alert_id: 'a3', severity: 'critical', status: 'closed', message: 'GPU busy back under 90%',
      metric_source: 'mac_power', metric_name: 'gpu_busy_pct', source_host: 'mac-studio', created_at: (NOW - 3600) * 1000 },
    { alert_id: 'a4', severity: 'info', status: 'active', category: 'info', message: 'Download complete',
      metric_source: 'manager', metric_name: 'downloads', created_at: (NOW - 10800) * 1000 },
  ];

  it('splits firing from earlier; active info alerts fire, closed ones are earlier', () => {
    const m = CView.alerts(list, NOW);
    expect(m.firing.map((r) => r.id)).toEqual(['a1', 'a2', 'a4']);
    expect(m.earlier.map((r) => r.id)).toEqual(['a3']);
    expect(m.earlier[0]).toMatchObject({ sev: 'ok', glyph: '✓', word: 'resolved' });
    expect(m.firing[2]).toMatchObject({ sev: 'info', glyph: 'i', word: 'info', info: true });
  });

  it('a closed info alert resolves rather than staying an info row', () => {
    const m = CView.alerts([{ ...list[3], status: 'closed' }], NOW);
    expect(m.firing).toEqual([]);
    expect(m.earlier[0]).toMatchObject({ sev: 'ok', word: 'resolved', info: false });
  });

  it('severity glyph + word + ack state', () => {
    const m = CView.alerts(list, NOW);
    expect(m.firing[0]).toMatchObject({ sev: 'crit', glyph: '!', word: 'critical · firing', ackable: true });
    expect(m.firing[0].meta).toBe('system/gpu_temp · llm-core · 2m ago');
    expect(m.firing[1]).toMatchObject({ sev: 'warn', word: 'warning · acked', ackable: false });
  });

  it('badge counts unacked firing alerts and never counts info', () => {
    // a1 active + a2 acknowledged + a4 info fire; only a1 is unread + actionable.
    const m = CView.alerts(list, NOW);
    expect(m.counts).toMatchObject({ badge: 1, critical: 1, warning: 1, info: 1 });
  });

  it('resolved and info rows never offer Ack, even when status is still active', () => {
    const m = CView.alerts(list, NOW);
    expect(m.firing.find((r) => r.id === 'a1').ackable).toBe(true);
    expect(m.firing.find((r) => r.id === 'a2').ackable).toBe(false);
    // a4: info alert the AE keeps status=active — listed, but no Ack.
    expect(m.firing.find((r) => r.id === 'a4').ackable).toBe(false);
    expect(m.earlier.find((r) => r.id === 'a3').ackable).toBe(false);
  });

  it('empty list yields zero counts', () => {
    expect(CView.alerts([]).counts.badge).toBe(0);
  });
});

describe('CView.admin', () => {
  // Real /api/agents record: last_heartbeat (ISO), bind_url (https = TLS);
  // NO last_heartbeat_age_s and NO tls field. AE service tls is a dict.
  const d = {
    version: 'v2026.08.07-4', now: NOW,
    agents: [
      { agent_id: 'core', hostname: 'llm-core', liveness: 'live', is_host_agent: true,
        bind_url: 'https://llm-core:8082', last_heartbeat: new Date((NOW - 3) * 1000).toISOString() },
      { agent_id: 'mac', hostname: 'mac-studio', liveness: 'live', version: 'v2026.07.30-3',
        bind_url: 'https://mac-studio:8082', last_heartbeat: new Date((NOW - 4) * 1000).toISOString() },
      { agent_id: 'new', hostname: 'mac-mini-m4', liveness: 'pending', status: 'pending' },
    ],
    health: {
      manager: { uptime_s: 3600 },
      services: [
        { name: 'alarm_engine', ok: true, tls: { enabled: true, active: true }, latency_ms: 6 },
        { name: 'influxdb', state: 'connected', via: 'co-located' },
      ],
    },
    backup: { enabled: true, keep_last: 14, last: { ok: true, ts: NOW - 20000 } },
    auth: { mode: 'session', current_user: 'llmadmin' },
  };

  it('agents: liveness, heartbeat age from last_heartbeat, TLS from bind_url, pending warn', () => {
    const a = CView.admin(d);
    expect(a.agents[0]).toMatchObject({ status: 'ok', name: 'llm-core', detail: 'local · manager host', right: 'live' });
    expect(a.agents[1]).toMatchObject({ right: 'live', rightSub: 'hb 4s', detail: 'agent v2026.07.30-3 · TLS' });
    expect(a.agents[2]).toMatchObject({ status: 'idle', right: 'pending', warn: true });
  });

  it('status rows: auth, influx ok, backups, AE reachable with TLS active', () => {
    const a = CView.admin(d);
    expect(a.rows[0]).toMatchObject({ name: 'Authentication', detail: 'session mode · llmadmin' });
    expect(a.rows[1]).toMatchObject({ name: 'InfluxDB', ok: true });
    expect(a.rows[3]).toMatchObject({ name: 'Alarm engine', ok: true });
    expect(a.rows[3].detail).toContain('TLS');
  });

  it('AE TLS badge reflects active state, not mere reachability', () => {
    // tls dict with active:false must NOT claim TLS (default deploy)
    const off = CView.admin({ ...d, health: { ...d.health,
      services: [{ name: 'alarm_engine', ok: true, tls: { enabled: false, active: false } },
                 { name: 'influxdb', state: 'connected' }] } });
    expect(off.rows[3].detail).toContain('reachable');
    expect(off.rows[3].detail).not.toContain('TLS');
  });

  it('manager version carries through', () => {
    expect(CView.admin(d).manager.version).toBe('v2026.08.07-4');
  });

  it('empty payload does not throw', () => {
    const a = CView.admin({});
    expect(a.agents).toEqual([]);
    expect(a.rows.length).toBe(4);
  });
});

describe('CView.actions', () => {
  const full = {
    llama: { state: 'awake', model: 'qwen3-32b-q4_k_m', agent_online: true, agent_age_s: 2 },
    // Real backend shapes: system-health carries ae_local/containerized, and
    // the agents global pin dict key is llama_model_pins (pin_dict_key).
    health: { manager: { uptime_s: 4 * 86400 }, ae_local: true, containerized: false,
              services: [{ name: 'alarm_engine', ok: true, tls: { enabled: true, active: true } }] },
    autopilot: { state: { enabled: true, entries: [{ model: 'a', provider: 'llama' },
      { model: 'b', provider: 'lms' }], hosts: {} }, proposals: [] },
    agents: { agents: [
      { agent_id: 'aa11', hostname: 'llm-core', status: 'approved', liveness: 'live' },
      { agent_id: 'bb22', hostname: 'mac-mini-m4', status: 'pending', liveness: 'pending',
        first_seen: new Date((NOW - 240) * 1000).toISOString(), version: 'v2026.07.30-3' },
    ], global: { llama_model_pins: { 'qwen3-32b-q4_k_m': 'aa11' }, primary_llama_id: 'aa11' } },
    version: 'v2026.08.08-1', now: NOW,
  };

  it('builds the service rows with manager + alarm engine on top', () => {
    const a = CView.actions(full);
    expect(a.services.map((s) => s.key)).toEqual(['manager', 'alarm_engine', 'llama']);
    expect(a.services[0].detail).toContain('v2026.08.08-1');
    expect(a.services[0].detail).toContain('4d');
    expect(a.services[1]).toMatchObject({ status: 'ok', canRestart: true });
    expect(a.services[1].detail).toContain('reachable');
    expect(a.services[2]).toMatchObject({ status: 'ok', canRestart: true });
    expect(a.services[2].detail).toContain('qwen3-32b-q4_k_m');
  });

  it('model row: resident + pinned via global.llama_pins', () => {
    const a = CView.actions(full);
    expect(a.model).toMatchObject({ name: 'qwen3-32b-q4_k_m', resident: true, pinned: true });
    expect(a.model.detail).toContain('pinned');
    expect(a.primaryLlamaId).toBe('aa11');
  });

  it('one model row per present provider, each carrying its own pin state', () => {
    const a = CView.actions({ ...full,
      lms: { agent_id: 'lm1', agent_online: true,
             ps: [{ model: 'nemotron-nano', status: 'IDLE' }] },
      vllm: { agent_id: 'vl1', agent_online: true,
              vllm: { state: 'running', model: 'mixtral' } },
      agents: { agents: [...full.agents.agents,
                  { agent_id: 'lm1', hostname: 'mac-studio', status: 'approved', liveness: 'live' }],
                global: { ...full.agents.global,
                  lms_model_pins: { 'nemotron-nano': 'lm1' } } } });
    expect(a.models.map((r) => r.key)).toEqual(['llama', 'lms', 'vllm']);
    expect(a.models[0]).toMatchObject({ model: 'qwen3-32b-q4_k_m', pinned: true,
      pinHost: 'llm-core', canSwap: true });
    expect(a.models[1]).toMatchObject({ model: 'nemotron-nano', pinned: true,
      agentId: 'lm1', canSwap: false });
    expect(a.models[1].detail).toContain('pinned → mac-studio');
    expect(a.models[2]).toMatchObject({ model: 'mixtral', pinned: false, canSwap: false });
  });

  it('pins list spans every provider, flagging pins whose model is not loaded', () => {
    const a = CView.actions({ ...full,
      agents: { ...full.agents, global: { ...full.agents.global,
        vllm_model_pins: { mixtral: 'aa11' } } } });
    expect(a.pins).toEqual([
      { provider: 'llama', label: 'llama.cpp', model: 'qwen3-32b-q4_k_m',
        agentId: 'aa11', host: 'llm-core', resident: true },
      { provider: 'vllm', label: 'vLLM', model: 'mixtral',
        agentId: 'aa11', host: 'llm-core', resident: false },
    ]);
  });

  it('autopilot exposes managed entries + settings, not just on/off', () => {
    const a = CView.actions({ ...full, autopilot: { ...full.autopilot,
      state: { enabled: true, hosts: {}, entries: [
        { model: 'a', provider: 'llama', placement: 'auto', failover: 'semi',
          min_replicas: 1, max_replicas: 1 },
        { model: 'b', provider: 'lms', placement: 'aa11', failover: 'auto',
          min_replicas: 1, max_replicas: 3 }] },
      entry_status: { 'a/llama': { placed: 1, want: 1, blocked: null },
                      'b/lms': { placed: 0, want: 1, blocked: 'no live agent supports this provider' } },
      proposals: [{ id: 'p1' }], last_plan_ts: NOW - 90 } });
    expect(a.autopilot).toMatchObject({ on: true });
    expect(a.autopilot.detail).toBe('2 models managed · 1/2 placed');
    expect(a.autopilot.entries[0]).toMatchObject({ model: 'a', right: '1/1', warn: false });
    expect(a.autopilot.entries[0].detail).toBe('llama.cpp · auto place · semi-auto · 1×');
    expect(a.autopilot.entries[1]).toMatchObject({ model: 'b', right: '0/1', warn: true });
    expect(a.autopilot.entries[1].detail)
      .toBe('LM Studio · host llm-core · hands-off · 1–3× · no live agent supports this provider');
    expect(a.autopilot.settings[0].detail).toContain('1 pending');
    expect(a.autopilot.settings[1].detail).toBe('2m ago');
  });

  it('autopilot with no declared models still reports cleanly', () => {
    const a = CView.actions({ ...full,
      autopilot: { state: { enabled: false, entries: [], hosts: {} } } });
    expect(a.autopilot).toMatchObject({ on: false, detail: 'no models declared' });
    expect(a.autopilot.entries).toEqual([]);
    expect(a.autopilot.settings[1].detail).toBe('not run yet');
  });

  it('pending agents card rows', () => {
    const a = CView.actions(full);
    expect(a.pending).toHaveLength(1);
    expect(a.pending[0]).toMatchObject({ id: 'bb22', name: 'mac-mini-m4' });
    expect(a.pending[0].detail).toContain('4m ago');
    expect(a.pending[0].detail).toContain('v2026.07.30-3');
  });

  it('LMS + vLLM service rows follow the provider block, after llama', () => {
    const a = CView.actions({ ...full,
      lms: { agent_id: 'lm1', agent_online: true,
             ps: [{ model: 'nemotron-nano', status: 'IDLE' }] },
      vllm: { agent_online: true, vllm: { state: 'running', model: 'mixtral' } } });
    expect(a.services.map((s) => s.key))
      .toEqual(['manager', 'alarm_engine', 'llama', 'lms', 'vllm']);
    expect(a.services[3]).toMatchObject({ status: 'ok', detail: 'nemotron-nano', canRestart: true });
    expect(a.services[4]).toMatchObject({ status: 'ok', detail: 'mixtral', canRestart: true });
  });

  it('no LMS/vLLM rows when no capable agent exists (agent_id null)', () => {
    const a = CView.actions({ ...full,
      lms: { agent_id: null, agent_online: false },
      vllm: { agent_id: null, agent_online: false } });
    expect(a.services.map((s) => s.key)).toEqual(['manager', 'alarm_engine', 'llama']);
    expect(a.models.map((r) => r.key)).toEqual(['llama']);
  });

  it('AE row mirrors the Manager row: version · uptime from AE /health passthrough', () => {
    const a = CView.actions({ ...full, health: { ...full.health,
      services: [{ name: 'alarm_engine', ok: true, version: 'v2026.08.08-1',
                   uptime_s: 12 * 86400 + 7200 }] } });
    expect(a.services[1].detail).toContain('v2026.08.08-1');
    expect(a.services[1].detail).toContain('12d');
    expect(a.services[1].detail).not.toContain('reachable');
    expect(a.services[1].detail).not.toContain('TLS');
  });

  it('AE stays restartable when unreachable — that is when restart matters', () => {
    const a = CView.actions({ ...full, health: { ...full.health,
      services: [{ name: 'alarm_engine', ok: false }] } });
    expect(a.services[1]).toMatchObject({ status: 'idle', canRestart: true });
    expect(a.services[1].detail).toContain('unreachable');
  });

  it('AE restart disabled when it runs on a separate host (no local unit, not containerized)', () => {
    const a = CView.actions({ ...full, health: { ...full.health, ae_local: false } });
    expect(a.services[1].canRestart).toBe(false);
  });

  it('agentsKnown false when the agents read failed, true when it returned', () => {
    expect(CView.actions(full).agentsKnown).toBe(true);
    expect(CView.actions({ ...full, agents: null }).agentsKnown).toBe(false);
  });

  it('gated: admin reads null → llama row live, admin rows disabled, no autopilot/pending', () => {
    const a = CView.actions({ llama: full.llama, health: null, autopilot: null, agents: null,
                              version: 'v1', now: NOW });
    expect(a.gated).toBe(true);
    expect(a.services[0].canRestart).toBe(false);
    expect(a.services[1].canRestart).toBe(false);
    expect(a.services[2].canRestart).toBe(true);
    expect(a.autopilot).toBeNull();
    expect(a.pending).toEqual([]);
    expect(a.pins).toEqual([]);
    expect(a.model.pinned).toBe(false);
  });

  it('empty world degrades, never throws', () => {
    const a = CView.actions({});
    expect(a.services).toHaveLength(3);
    expect(a.services[2].status).toBe('idle');
    expect(a.models).toHaveLength(1);
    expect(a.model.name).toBeNull();
    expect(a.model.resident).toBe(false);
  });
});
