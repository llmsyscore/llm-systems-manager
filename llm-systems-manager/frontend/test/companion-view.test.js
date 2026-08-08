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

  it('LM Studio host comes from system.host (not hostname), never a raw UUID', () => {
    // Regression: the agent system block key is `host`; `hostname` is absent.
    const g = CView.glance({ lms: { ps: [{ model: 'm', status: 'loaded' }],
      system: { host: 'mac-studio' }, agent_id: '7f3c-uuid-long' } });
    expect(g.providers[1].detail).toBe('mac-studio · m');
  });

  it('vLLM running shows model + request count', () => {
    const g = CView.glance({ vllm: { vllm: { state: 'running', model: 'mixtral',
      requests_running: 3, tokens_per_second: 55 } } });
    expect(g.providers[2]).toMatchObject({ status: 'ok', detail: 'mixtral', rN: '3', rUnit: 'running' });
    expect(g.hero.label).toBe('vLLM · mixtral');
  });

  it('provider rows: llama ctx + slots, lms host + model count, vllm idle', () => {
    const g = CView.glance(full);
    expect(g.providers[0]).toMatchObject({ status: 'ok', name: 'llama.cpp',
      detail: 'qwen3-32b-q4_k_m · ctx 32k', rN: '2/4', rUnit: 'slots' });
    expect(g.providers[1]).toMatchObject({ status: 'ok', name: 'LM Studio',
      detail: 'mac-studio · llama-3.3-70b', rN: '1', rUnit: 'model' });
    expect(g.providers[2]).toMatchObject({ status: 'idle', name: 'vLLM', detail: 'unit stopped' });
  });

  it('LMS: an IDLE ps row still counts as a loaded model (real lms ps shape)', () => {
    // Regression: real `lms ps` reports loaded-but-not-generating as "IDLE".
    const g = CView.glance({ lms: { ps: [{ model: 'nvidia/nemotron-3-nano-4b', status: 'IDLE' }],
      system: { host: 'mac-studio' } } });
    expect(g.providers[1]).toMatchObject({ status: 'ok',
      detail: 'mac-studio · nvidia/nemotron-3-nano-4b', rN: '1', rUnit: 'model' });
  });

  it('system tiles: temp, VRAM with total + hot flag, PSU power, energy today', () => {
    const g = CView.glance(full);
    expect(g.tiles[0]).toMatchObject({ v: '62', unit: '°C', meter: 62, hot: false });
    expect(g.tiles[1]).toMatchObject({ v: '21.4', unit: ' / 24 GB', meter: 89, hot: true });
    expect(g.tiles[2]).toMatchObject({ v: '412', unit: 'W', sub: 'PSU · liquidctl' });
    expect(g.tiles[3]).toMatchObject({ v: '$1.84', sub: '9.2 kWh · $0.2/kWh' });
  });

  it('degrades to dashes/idle on an empty payload without throwing', () => {
    const g = CView.glance({});
    expect(g.hero.n).toBe('0.0');
    expect(g.hero.label).toContain('idle');
    expect(g.providers.every((p) => p.status === 'idle')).toBe(true);
    expect(g.tiles[0].v).toBe('—');
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

  it('splits firing from earlier, resolved shows ✓, info counts as earlier', () => {
    const m = CView.alerts(list, NOW);
    expect(m.firing.map((r) => r.id)).toEqual(['a1', 'a2']);
    expect(m.earlier.map((r) => r.id)).toEqual(['a3', 'a4']);
    expect(m.earlier[0]).toMatchObject({ sev: 'ok', glyph: '✓', word: 'resolved' });
    expect(m.earlier[1]).toMatchObject({ glyph: '✓', word: 'info' });
  });

  it('severity glyph + word + ack state', () => {
    const m = CView.alerts(list, NOW);
    expect(m.firing[0]).toMatchObject({ sev: 'crit', glyph: '!', word: 'critical · firing', ackable: true });
    expect(m.firing[0].meta).toBe('system/gpu_temp · llm-core · 2m ago');
    expect(m.firing[1]).toMatchObject({ sev: 'warn', word: 'warning · acked', ackable: false });
  });

  it('badge counts only unacked firing alerts', () => {
    // a1 active + a2 acknowledged fire, but the badge is the unread count.
    const m = CView.alerts(list, NOW);
    expect(m.counts).toMatchObject({ badge: 1, critical: 1, warning: 1 });
  });

  it('resolved and info rows never offer Ack, even when status is still active', () => {
    const m = CView.alerts(list, NOW);
    expect(m.firing.find((r) => r.id === 'a1').ackable).toBe(true);
    expect(m.firing.find((r) => r.id === 'a2').ackable).toBe(false);
    // a4: info alert the AE keeps status=active — shown in Earlier, no Ack.
    expect(m.earlier.find((r) => r.id === 'a4').ackable).toBe(false);
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

  it('builds the three service rows, llama first', () => {
    const a = CView.actions(full);
    expect(a.services.map((s) => s.key)).toEqual(['llama', 'manager', 'alarm_engine']);
    expect(a.services[0]).toMatchObject({ status: 'ok', canRestart: true });
    expect(a.services[0].detail).toContain('qwen3-32b-q4_k_m');
    expect(a.services[1].detail).toContain('v2026.08.08-1');
    expect(a.services[1].detail).toContain('4d');
    expect(a.services[2]).toMatchObject({ status: 'ok', canRestart: true });
    expect(a.services[2].detail).toContain('reachable');
  });

  it('model row: resident + pinned via global.llama_pins', () => {
    const a = CView.actions(full);
    expect(a.model).toMatchObject({ name: 'qwen3-32b-q4_k_m', resident: true, pinned: true });
    expect(a.model.detail).toContain('pinned');
    expect(a.primaryLlamaId).toBe('aa11');
  });

  it('autopilot on with entry count', () => {
    const a = CView.actions(full);
    expect(a.autopilot).toMatchObject({ on: true });
    expect(a.autopilot.detail).toContain('2 models');
  });

  it('pending agents card rows', () => {
    const a = CView.actions(full);
    expect(a.pending).toHaveLength(1);
    expect(a.pending[0]).toMatchObject({ id: 'bb22', name: 'mac-mini-m4' });
    expect(a.pending[0].detail).toContain('4m ago');
    expect(a.pending[0].detail).toContain('v2026.07.30-3');
  });

  it('LMS + vLLM service rows appear between llama and manager when reported', () => {
    const a = CView.actions({ ...full,
      lms: { agent_id: 'lm1', agent_online: true,
             ps: [{ model: 'nemotron-nano', status: 'IDLE' }] },
      vllm: { agent_online: true, vllm: { state: 'running', model: 'mixtral' } } });
    expect(a.services.map((s) => s.key))
      .toEqual(['llama', 'lms', 'vllm', 'manager', 'alarm_engine']);
    expect(a.services[1]).toMatchObject({ status: 'ok', detail: 'nemotron-nano', canRestart: true });
    expect(a.services[2]).toMatchObject({ status: 'ok', detail: 'mixtral', canRestart: true });
  });

  it('no LMS/vLLM rows when no capable agent exists (agent_id null)', () => {
    const a = CView.actions({ ...full,
      lms: { agent_id: null, agent_online: false },
      vllm: { agent_id: null, agent_online: false } });
    expect(a.services.map((s) => s.key)).toEqual(['llama', 'manager', 'alarm_engine']);
  });

  it('AE row mirrors the Manager row: version · uptime from AE /health passthrough', () => {
    const a = CView.actions({ ...full, health: { ...full.health,
      services: [{ name: 'alarm_engine', ok: true, version: 'v2026.08.08-1',
                   uptime_s: 12 * 86400 + 7200 }] } });
    expect(a.services[2].detail).toContain('v2026.08.08-1');
    expect(a.services[2].detail).toContain('12d');
    expect(a.services[2].detail).not.toContain('reachable');
    expect(a.services[2].detail).not.toContain('TLS');
  });

  it('AE stays restartable when unreachable — that is when restart matters', () => {
    const a = CView.actions({ ...full, health: { ...full.health,
      services: [{ name: 'alarm_engine', ok: false }] } });
    expect(a.services[2]).toMatchObject({ status: 'idle', canRestart: true });
    expect(a.services[2].detail).toContain('unreachable');
  });

  it('AE restart disabled when it runs on a separate host (no local unit, not containerized)', () => {
    const a = CView.actions({ ...full, health: { ...full.health, ae_local: false } });
    expect(a.services[2].canRestart).toBe(false);
  });

  it('agentsKnown false when the agents read failed, true when it returned', () => {
    expect(CView.actions(full).agentsKnown).toBe(true);
    expect(CView.actions({ ...full, agents: null }).agentsKnown).toBe(false);
  });

  it('gated: admin reads null → llama row live, admin rows disabled, no autopilot/pending', () => {
    const a = CView.actions({ llama: full.llama, health: null, autopilot: null, agents: null,
                              version: 'v1', now: NOW });
    expect(a.gated).toBe(true);
    expect(a.services[0].canRestart).toBe(true);
    expect(a.services[1].canRestart).toBe(false);
    expect(a.services[2].canRestart).toBe(false);
    expect(a.autopilot).toBeNull();
    expect(a.pending).toEqual([]);
    expect(a.model.pinned).toBe(false);
  });

  it('empty world degrades, never throws', () => {
    const a = CView.actions({});
    expect(a.services).toHaveLength(3);
    expect(a.services[0].status).toBe('idle');
    expect(a.model.name).toBeNull();
    expect(a.model.resident).toBe(false);
  });
});
