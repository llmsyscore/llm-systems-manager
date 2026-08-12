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
  it('tsSeconds normalizes every timestamp shape the APIs emit', () => {
    expect(CView.tsSeconds(NOW)).toBe(NOW);
    expect(CView.tsSeconds(NOW * 1000)).toBe(NOW);
    expect(CView.tsSeconds('2026-08-09T17:45:41Z')).toBe(Date.parse('2026-08-09T17:45:41Z') / 1000);
    // zoneless (alarm engine / influx history) must be read as UTC, not local
    expect(CView.tsSeconds('2026-08-09T17:45:41')).toBe(Date.parse('2026-08-09T17:45:41Z') / 1000);
    expect(CView.tsSeconds('nope')).toBeNull();
    expect(CView.tsSeconds(null)).toBeNull();
  });

  it('reads the alarm engine\'s zoneless naive-UTC strings as UTC', () => {
    // The AE emits "2026-08-09T17:45:41.669823" with no zone. Date.parse reads
    // a zoneless date-time as LOCAL, which put every alert in the future and
    // clamped every age to "just now". Must hold under any host TZ.
    const utcNow = Date.parse('2026-08-09T19:45:41Z') / 1000;
    expect(CView.age('2026-08-09T17:45:41.669823', utcNow)).toBe('2h ago');
    expect(CView.age('2026-08-09T19:15:41', utcNow)).toBe('30m ago');
    // Explicit offsets must still be honoured, not double-suffixed.
    expect(CView.age('2026-08-09T17:45:41+00:00', utcNow)).toBe('2h ago');
    expect(CView.age('2026-08-09T19:45:41Z', utcNow)).toBe('just now');
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
    // Every row reads gpu / cpu / ram for its own host.
    expect(g.providers.map((p) => p.stats.map((s) => s.k)))
      .toEqual([['gpu', 'cpu', 'ram'], ['gpu', 'cpu', 'ram'], ['gpu', 'cpu', 'ram']]);
    expect(g.providers[0]).toMatchObject({ status: 'ok', name: 'llama.cpp',
      detail: 'generating · qwen3-32b-q4_k_m' });
    expect(g.providers[0].stats[0]).toEqual({ k: 'gpu', v: '96%' });
    expect(g.providers[1]).toMatchObject({ status: 'ok', name: 'LM Studio',
      detail: 'generating · llama-3.3-70b' });
    expect(g.providers[1].stats[0]).toEqual({ k: 'gpu', v: '71%' });
    expect(g.providers[2]).toMatchObject({ status: 'ok', name: 'vLLM',
      detail: 'idle · mixtral' });
    expect(g.providers[2].stats[0]).toEqual({ k: 'gpu', v: '12%' });
  });

  it('a provider with no GPU telemetry dashes its GPU cell, keeps its status', () => {
    const g = CView.glance(full);
    expect(g.providers[2]).toMatchObject({ status: 'idle', name: 'vLLM',
      detail: 'stopped' });
    expect(g.providers[2].stats).toEqual([{ k: 'gpu', v: '—' },
      { k: 'cpu', v: '—' }, { k: 'ram', v: '—' }]);
  });

  it('LMS: an IDLE ps row is loaded-not-generating, not absent', () => {
    // Regression: real `lms ps` reports loaded-but-not-generating as "IDLE".
    const g = CView.glance({ lms: { ps: [{ model: 'nvidia/nemotron-3-nano-4b', status: 'IDLE' }],
      system: { host: 'mac-studio' }, mac_power: { gpu_busy_pct: 4 } } });
    expect(g.providers[1]).toMatchObject({ status: 'ok',
      detail: 'idle · nvidia/nemotron-3-nano-4b' });
    expect(g.providers[1].stats[0]).toEqual({ k: 'gpu', v: '4%' });
  });

  it('power is its own pair: live input watts + today\'s energy', () => {
    const g = CView.glance(full);
    expect(g.power).toHaveLength(2);
    expect(g.power[0]).toMatchObject({ v: '412', unit: 'W', k: 'Input power',
      sub: 'PSU · 1 host' });
    expect(g.power[1]).toMatchObject({ v: '$1.84', k: 'Energy today',
      sub: '9.2 kWh · $0.2/kWh' });
  });

  it('input power sums every reporting host, deduped, not just llama.cpp', () => {
    const g = CView.glance({ ...full,
      lms: { ...full.lms, mac_power: { gpu_busy_pct: 71, soc_total_w: 38 } } });
    expect(g.power[0]).toMatchObject({ v: '450', unit: 'W', sub: 'PSU + SoC · 2 hosts' });
  });

  it('input power falls back to the energy accumulator when no sample has watts', () => {
    const g = CView.glance({ energy: { totals: { avg_watts: 118.4 } } });
    expect(g.power[0]).toMatchObject({ v: '118', sub: 'fleet avg · this window' });
  });

  it('degrades to dashes/idle on an empty payload without throwing', () => {
    const g = CView.glance({});
    expect(g.hero.n).toBe('0.0');
    expect(g.hero.label).toContain('idle');
    expect(g.providers.every((p) => p.status === 'idle')).toBe(true);
    expect(g.providers.every((p) => p.stats.every((s) => s.v === '—'))).toBe(true);
    expect(g.system).toEqual([]);
    expect(g.power[0]).toMatchObject({ v: '—', sub: 'no telemetry' });
    expect(g.power[1].sub).toBe('no telemetry');
  });

  it('flags a hot GPU at the warn threshold', () => {
    const g = CView.glance({ metrics: { gpu: { temperature_c: 87 } },
      trends: [{ key: 'gpu_temp', name: 'GPU temp', unit: '°C', dp: 0, pts: [], last: 60 }] });
    expect(g.system[0]).toMatchObject({ live: 87, hot: true });
  });

  // One card per metric: headline is NOW, the series beneath it is the last
  // 24 h. Previously these lived in two grids and GPU temp / VRAM appeared in
  // both, showing two different numbers for the same thing.
  describe('system cards merge the live reading with its 24 h series', () => {
    const trends = [
      { key: 'gpu_temp', name: 'GPU temp', unit: '°C', dp: 0, pts: [], last: 56,
        min: 52, max: 59 },
      { key: 'gpu_vram', name: 'VRAM', unit: '%', dp: 0, pts: [], last: 19,
        min: 19, max: 19 },
    ];

    it('carries the 24 h series through alongside the live headline', () => {
      const g = CView.glance({
        metrics: { gpu: { temperature_c: 71 } }, trends });
      expect(g.system[0]).toMatchObject({ name: 'GPU temp', live: 71,
        min: 52, max: 59 });
    });

    it('falls back to the newest history sample when the live block has none', () => {
      // The gpu block belongs to the PRIMARY llama agent, which need not be
      // the host that has the GPU.
      const g = CView.glance({ metrics: {}, trends });
      expect(g.system.map((c) => c.live)).toEqual([56, 19]);
    });

    it('keeps the GB detail as a sub-line extra, not as the headline', () => {
      const g = CView.glance({
        metrics: { gpu: { vram_used_mb: 12288, vram_usage_percent: 50 } },
        trends });
      expect(g.system[1]).toMatchObject({ live: 50, unit: '%',
        extra: '12 / 24 GB' });
    });

    it('takes absolute VRAM from fleet history when the live block has none', () => {
      // The host with the GPU need not be the primary llama agent, so the
      // live payloads can carry a percent with no bytes behind it.
      const g = CView.glance({ metrics: {}, trends,
        latest: { gpu_vram_used: 12 * 1073741824, gpu_vram_total: 24 * 1073741824 } });
      expect(g.system[1].extra).toBe('12 / 24 GB');
    });

    it('keeps a decimal on a small GPU, where rounding would read as zero', () => {
      const g = CView.glance({ metrics: {}, trends,
        latest: { gpu_vram_used: 102031360, gpu_vram_total: 536870912 } });
      expect(g.system[1].extra).toBe('0.1 / 0.5 GB');
    });

    it('shows used alone when no total is known', () => {
      const g = CView.glance({ metrics: {}, trends,
        latest: { gpu_vram_used: 12 * 1073741824 } });
      expect(g.system[1].extra).toBe('12 GB');
    });

    it('has no extra when neither source carries a GB figure', () => {
      expect(CView.glance({ metrics: {}, trends }).system[1].extra).toBe('');
    });

    it('a metric with neither source reads as a dash-able null', () => {
      const g = CView.glance({ metrics: {},
        trends: [{ key: 'gpu_temp', name: 'GPU temp', unit: '°C', dp: 0, pts: [] }] });
      expect(g.system[0].live).toBeUndefined();
    });

    it('GPU busy and CPU take the busiest provider host, live', () => {
      const g = CView.glance({
        metrics: { gpu: { gpu_util_percent: 30 }, cpu_total: 4 },
        lms: { ps: [{ model: 'm', status: 'IDLE' }], system: { cpu_total: 22 },
          mac_power: { gpu_busy_pct: 99 } },
        trends: [
          { key: 'gpu_util', name: 'GPU busy', unit: '%', dp: 0, pts: [], last: 5 },
          { key: 'cpu_total', name: 'CPU', unit: '%', dp: 0, pts: [], last: 3 },
        ] });
      expect(g.system[0].live).toBe(99);
      expect(g.system[1].live).toBe(22);
    });
  });

  // #541: cross-provider summary above the per-provider rows.
  describe('fleet tiles', () => {
    const busy = {
      metrics: { llama: { tokens_per_second: 42.7, prompt_tokens_per_second: 300,
                          requests_processing: 2, active_slots: 3,
                          requests_deferred: 1, model: 'qwen3-32b' } },
      llama: { state: 'awake', model: 'qwen3-32b' },
      lms: { ps: [{ model: 'llama-3.3-70b', status: 'loaded' }],
             gateway_rates: { gen_tps: 12, prompt_tps: 40 } },
      vllm: { vllm: { state: 'running', model: 'mixtral', tokens_per_second: 5,
                      prompt_tokens_per_second: 10, requests_running: 4,
                      requests_waiting: 2 } },
    };

    // Rates read 0 between requests, so the headline is work done over the
    // window, integrated from the rate series.
    it('generated reads 24 h TOTAL tokens, not the instantaneous rate', () => {
      const hist = [];
      for (let i = 0; i <= 24; i++) hist.push({ t: NOW - (24 - i) * 3600, v: 10, p: 2 });
      const f = CView.glance({ ...busy, hist }).fleet;
      // 10 tok/s across 24 one-hour steps = 864 000 generated, 172 800 prompt.
      expect(f[0]).toMatchObject({ k: 'Generated · 24 h', v: '864k', unit: 'tokens' });
      expect(f[0].sub).toBe('avg 10.0 tok/s · prompt 173k');
    });

    it('a reporting gap is not counted as work done', () => {
      // 2 h hole: integrating straight across would invent 72 000 tokens.
      const f = CView.glance({ ...busy, hist: [
        { t: NOW - 10800, v: 10, p: 0 }, { t: NOW - 7200, v: 10, p: 0 },
        { t: NOW, v: 10, p: 0 },
      ] }).fleet;
      expect(f[0].v).toBe('36k');
    });

    it('falls back to the live rate when there is no history', () => {
      const f = CView.glance(busy).fleet;
      expect(f[0].v).toBe('—');
      expect(f[0].sub).toBe('idle · 59.7 tok/s now');
    });

    it('in-flight takes llama\'s larger of slots/processing plus vLLM', () => {
      const f = CView.glance(busy).fleet;
      expect(f[1]).toMatchObject({ k: 'In flight', v: '7', unit: 'req',
        sub: '3 queued' });
    });

    // LM Studio publishes no request or queue counters, so its share comes
    // from the manager gateway's in-flight tally; without it the card read 0
    // while the LMS host was serving.
    it('counts LM Studio via the gateway in-flight tally', () => {
      const f = CView.glance({ ...busy,
        lms: { ...busy.lms, gateway_inflight: 4 } }).fleet;
      expect(f[1].v).toBe('11');   // llama 3 + vLLM 4 + LM Studio 4
    });

    it('names the providers whose requests are actually counted', () => {
      const f = CView.glance({
        lms: { ps: [{ model: 'm', status: 'IDLE' }], gateway_inflight: 0 },
      }).fleet;
      expect(f[1]).toMatchObject({ v: '0', sub: 'via LM Studio' });
    });

    it('a provider with no counter is left out of the coverage line', () => {
      const f = CView.glance({
        metrics: { llama: { active_slots: 2 } },
        lms: { ps: [{ model: 'm', status: 'IDLE' }] },   // no gateway_inflight
      }).fleet;
      expect(f[1]).toMatchObject({ v: '2', sub: 'via llama.cpp' });
    });

    it('loaded counts and names the providers actually serving a model', () => {
      const f = CView.glance(busy).fleet;
      expect(f[2]).toMatchObject({ k: 'Loaded', v: '3', unit: 'models' });
      expect(f[2].sub).toBe('llama.cpp + LM Studio + vLLM');
    });

    it('the 24 h peak measures generation — the same thing Throughput leads with', () => {
      const f = CView.glance({ ...busy, hist: [
        { t: NOW - 7200, v: 10, p: 500 },
        { t: NOW - 3600, v: 80, p: 20 },
        { t: NOW, v: 30, p: 1 },
      ] }).fleet;
      expect(f[3]).toMatchObject({ k: '24 h peak', v: '80', unit: 'tok/s' });
      expect(f[3].sub).toMatch(/^at /);
    });

    it('an idle fleet says so rather than showing dashes everywhere', () => {
      const f = CView.glance({ metrics: { llama: { tokens_per_second: 0 } } }).fleet;
      expect(f[0]).toMatchObject({ v: '—', sub: 'idle · 0.0 tok/s now' });
      // No provider reported a request counter at all — say that, rather
      // than letting a 0 read as "nothing is running".
      expect(f[1]).toMatchObject({ v: '—', sub: 'no request counters' });
      expect(f[2]).toMatchObject({ v: '0', unit: 'models', sub: 'none loaded' });
      expect(f[3]).toMatchObject({ v: '—', sub: 'no history yet' });
    });

    it('an empty payload dashes every unknown, rather than claiming zero', () => {
      const f = CView.glance({}).fleet;
      expect(f).toHaveLength(4);
      expect(f.map((t) => t.v)).toEqual(['—', '—', '0', '—']);
    });
  });
});

describe('CView.trends', () => {
  const rows = [
    { ts: NOW - 7200, gpu_util: 10, gpu_temp: 50, gpu_vram: 40, cpu_total: 5 },
    { ts: NOW - 3600, gpu_util: 90, gpu_temp: 71, gpu_vram: 88, cpu_total: 30 },
    { ts: NOW, gpu_util: 55, gpu_temp: 64, gpu_vram: 61, cpu_total: 12 },
  ];

  it('returns one card per fleet metric with the window mean and range', () => {
    const t = CView.trends(rows);
    expect(t.map((x) => x.key)).toEqual(['gpu_util', 'gpu_temp', 'gpu_vram', 'cpu_total']);
    // Mean, not the newest sample: history lags the live tiles by a bucket.
    expect(t[0]).toMatchObject({ name: 'GPU busy', unit: '%', min: 10, max: 90 });
    expect(t[0].avg).toBeCloseTo((10 + 90 + 55) / 3);
    expect(t[1]).toMatchObject({ unit: '°C', min: 50, max: 71 });
    expect(t[1].avg).toBeCloseTo((50 + 71 + 64) / 3);
    expect(t[0].pts).toHaveLength(3);
    // `last` backs the System tiles when the live GPU block is absent.
    expect(t[0].last).toBe(55);
    expect(t[1].last).toBe(64);
  });

  it('drops a metric the history never carried rather than drawing a flat line', () => {
    const t = CView.trends(rows.map(({ ts, gpu_util }) => ({ ts, gpu_util })));
    expect(t.map((x) => x.key)).toEqual(['gpu_util']);
  });

  it('skips null samples but keeps the rest of the series', () => {
    const t = CView.trends([
      { ts: NOW - 60, gpu_util: 10 }, { ts: NOW - 30, gpu_util: null },
      { ts: NOW, gpu_util: 20 },
    ]);
    expect(t[0].pts.map((p) => p.v)).toEqual([10, 20]);
  });

  it('needs two points before a card is worth drawing', () => {
    expect(CView.trends([{ ts: NOW, gpu_util: 10 }])).toEqual([]);
  });

  // GPU residency arrives under a different metric name per GPU family; the
  // card read only the CUDA one, so an Apple host at 100% showed as ~5%.
  it('GPU busy takes the busiest of the CUDA and Apple Silicon series', () => {
    const t = CView.trends([
      { ts: NOW - 600, gpu_util: 5, mac_gpu_busy: 99 },
      { ts: NOW, gpu_util: 12, mac_gpu_busy: 40 },
    ]);
    expect(t[0].key).toBe('gpu_util');
    expect(t[0].pts.map((p) => p.v)).toEqual([99, 40]);
    expect(t[0].max).toBe(99);
  });

  it('a fleet with only Apple GPUs still plots a GPU busy card', () => {
    const t = CView.trends([
      { ts: NOW - 600, mac_gpu_busy: 30 }, { ts: NOW, mac_gpu_busy: 100 },
    ]);
    expect(t.map((x) => x.key)).toEqual(['gpu_util']);
    expect(t[0].pts.map((p) => p.v)).toEqual([30, 100]);
  });

  it('a fleet with only CUDA GPUs is unaffected', () => {
    const t = CView.trends([
      { ts: NOW - 600, gpu_util: 30 }, { ts: NOW, gpu_util: 70 },
    ]);
    expect(t[0].pts.map((p) => p.v)).toEqual([30, 70]);
  });

  it('parses the timestamp shapes /api/history emits', () => {
    const iso = new Date(NOW * 1000).toISOString();
    const t = CView.trends([{ ts: iso, gpu_util: 1 },
      { ts: (NOW + 60) * 1000, gpu_util: 2 }]);
    expect(t[0].pts.map((p) => p.t)).toEqual([NOW, NOW + 60]);
  });

  it('degrades to an empty list on junk input', () => {
    expect(CView.trends(null)).toEqual([]);
    expect(CView.trends([null, undefined, {}])).toEqual([]);
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

  it('names the rule that fired, without repeating the message', () => {
    const rows = CView.alerts([
      { ...list[0], rule_name: 'GPU over 90 °C' },
      { ...list[1], rule_name: 'VRAM 95% for 10 min' },   // same as its message
      { ...list[3] },                                      // no rule at all
    ], NOW).firing;
    expect(rows[0].rule).toBe('GPU over 90 °C');
    expect(rows[0].msg).toBe('GPU temperature 91 °C ≥ 90 °C');
    expect(rows[1].rule).toBe('');
    expect(rows[2].rule).toBe('');
  });

  it('active rows lead with the rule; resolved and rule-less rows do not', () => {
    const m = CView.alerts([
      { ...list[0], rule_name: 'GPU over 90 °C' },
      { ...list[1], rule_name: 'VRAM 95% for 10 min' },   // suppressed rule
      { ...list[2], rule_name: 'Disk almost full' },      // resolved
    ], NOW);
    expect(m.firing[0].ruleFirst).toBe(true);
    expect(m.firing[1].ruleFirst).toBe(false);
    expect(m.earlier[0].ruleFirst).toBe(false);
  });

  it('splits firing from earlier; active info alerts fire, closed ones are earlier', () => {
    const m = CView.alerts(list, NOW);
    expect(m.firing.map((r) => r.id)).toEqual(['a1', 'a2', 'a4']);
    expect(m.earlier.map((r) => r.id)).toEqual(['a3']);
    expect(m.earlier[0]).toMatchObject({ sev: 'ok', glyph: '✓' });
    expect(m.firing[2]).toMatchObject({ sev: 'info', glyph: 'i', word: 'info', info: true });
  });

  it('a resolved alert keeps its original severity in the word and the tone', () => {
    // Regression: resolved rows read a bare "resolved" and lost whether the
    // thing that fired was critical or a warning.
    const m = CView.alerts(list, NOW);
    expect(m.earlier[0]).toMatchObject({ word: 'critical · resolved', tone: 'crit', sev: 'ok' });
  });

  it('a resolved alert is dated from closed_at, not created_at', () => {
    const closed = { ...list[0], status: 'closed',
      created_at: (NOW - 7200) * 1000, closed_at: (NOW - 600) * 1000 };
    const m = CView.alerts([closed], NOW);
    expect(m.earlier[0].meta).toContain('10m ago');
    expect(m.earlier[0].meta).not.toContain('2h ago');
  });

  it('a closed info alert resolves rather than staying an info row', () => {
    const m = CView.alerts([{ ...list[3], status: 'closed' }], NOW);
    expect(m.firing).toEqual([]);
    expect(m.earlier[0]).toMatchObject({ sev: 'ok', word: 'info · resolved', info: false });
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

  it('agents: liveness, heartbeat age from last_heartbeat, TLS from bind_url', () => {
    const a = CView.admin(d);
    expect(a.agents[0]).toMatchObject({ status: 'ok', name: 'llm-core', right: 'live', id: 'core' });
    expect(a.agents[1]).toMatchObject({ right: 'live', rightSub: 'hb 4s', detail: 'agent v2026.07.30-3 · TLS' });
  });

  it('pending agents split out of the agent list into their own cards', () => {
    const a = CView.admin(d);
    expect(a.agents.map((x) => x.name)).not.toContain('mac-mini-m4');
    expect(a.pending).toHaveLength(1);
    expect(a.pending[0]).toMatchObject({ id: 'new', name: 'mac-mini-m4' });
  });

  it('no pending agents yields an empty list, not a placeholder row', () => {
    const a = CView.admin({ ...d, agents: d.agents.slice(0, 2) });
    expect(a.pending).toEqual([]);
  });

  it('a down agent gets the red state, not the grey idle one', () => {
    const a = CView.admin({ ...d,
      agents: [{ ...d.agents[1], liveness: 'down' }] });
    expect(a.agents[0]).toMatchObject({ status: 'down', warn: true });
  });

  it('services card carries manager, alarm engine and InfluxDB with versions', () => {
    const a = CView.admin(d);
    expect(a.services.map((s) => s.key)).toEqual(['manager', 'alarm_engine', 'influxdb']);
    expect(a.services[0]).toMatchObject({ name: 'Manager', canRestart: true });
    expect(a.services[0].detail).toContain('v2026.08.07-4');
    expect(a.services[1].detail).toContain('TLS');
    expect(a.services[2]).toMatchObject({ name: 'InfluxDB', status: 'ok', canRestart: false });
  });

  it('InfluxDB shows its version when system-health passes one through', () => {
    const a = CView.admin({ ...d, health: { ...d.health, services: [
      { name: 'alarm_engine', ok: true },
      { name: 'influxdb', state: 'connected', version: 'v2.9.1', ping_ms: 1.2 }] } });
    expect(a.services[2].detail).toContain('v2.9.1');
    expect(a.services[2].detail).toContain('1.2ms');
  });

  it('an unreachable alarm engine reads down and stays restartable', () => {
    const a = CView.admin({ ...d, health: { ...d.health, ae_local: true,
      services: [{ name: 'alarm_engine', ok: false }, { name: 'influxdb', state: 'connected' }] } });
    expect(a.services[1]).toMatchObject({ status: 'down', canRestart: true });
    expect(a.services[1].detail).toBe('unreachable');
  });

  it('the release note rides on the Manager row when the check is on', () => {
    expect(CView.admin({ ...d, releaseNote: 'v1.2.0 available' }).services[0].right)
      .toBe('v1.2.0 available');
    expect(CView.admin(d).services[0].right).toBe('');
  });

  it('the manager host agent shows its version too, not just its role', () => {
    // Regression: the is_host_agent branch printed only "local · manager host"
    // and threw away the version every other row displays.
    const a = CView.admin({ ...d, agents: [{ ...d.agents[0], version: 'v2026.08.09-1' }] });
    expect(a.agents[0].detail).toBe('local · manager host · agent v2026.08.09-1');
  });

  it('manager update note counts outdated agents instead of always saying up to date', () => {
    // Regression: companion.js never passed updateAvailable, so the row was
    // hardcoded to "up to date" in every deployment.
    expect(CView.admin(d).manager.updateNote).toBe('');
    expect(CView.admin({ ...d, agentUpdates: 2 }).manager.updateNote).toBe('2 agents outdated');
    expect(CView.admin({ ...d, agentUpdates: 1 }).manager.updateNote).toBe('1 agent outdated');
  });

  it('status rows keep auth and backups; influx and AE moved to the services card', () => {
    const a = CView.admin(d);
    expect(a.rows.map((r) => r.name)).toEqual(['Authentication', 'Backups']);
    expect(a.rows[0]).toMatchObject({ name: 'Authentication', detail: 'session mode · llmadmin' });
  });

  it('AE TLS badge reflects active state, not mere reachability', () => {
    // tls dict with active:false must NOT claim TLS (default deploy)
    const off = CView.admin({ ...d, health: { ...d.health,
      services: [{ name: 'alarm_engine', ok: true, tls: { enabled: false, active: false } },
                 { name: 'influxdb', state: 'connected' }] } });
    expect(off.services[1].detail).toContain('reachable');
    expect(off.services[1].detail).not.toContain('TLS');
  });

  it('manager version carries through', () => {
    expect(CView.admin(d).manager.version).toBe('v2026.08.07-4');
  });

  it('empty payload does not throw', () => {
    const a = CView.admin({});
    expect(a.agents).toEqual([]);
    expect(a.pending).toEqual([]);
    expect(a.rows.length).toBe(2);
    expect(a.services.map((s) => s.key)).toEqual(['manager', 'alarm_engine', 'influxdb']);
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

  it('services are provider units only — manager and AE live on Admin now', () => {
    const a = CView.actions(full);
    expect(a.services.map((s) => s.key)).toEqual(['llama']);
    expect(a.services[0]).toMatchObject({ status: 'ok', canRestart: true });
    expect(a.services[0].detail).toContain('qwen3-32b-q4_k_m');
  });

  it('an offline provider agent reads down, not idle', () => {
    const a = CView.actions({ ...full,
      llama: { ...full.llama, agent_online: false } });
    expect(a.services[0]).toMatchObject({ status: 'down', canRestart: false });
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

  it('pending agents are no longer this screen\'s business', () => {
    expect(CView.actions(full).pending).toBeUndefined();
  });

  it('LMS + vLLM service rows follow llama', () => {
    const a = CView.actions({ ...full,
      lms: { agent_id: 'lm1', agent_online: true,
             ps: [{ model: 'nemotron-nano', status: 'IDLE' }] },
      vllm: { agent_online: true, vllm: { state: 'running', model: 'mixtral' } } });
    expect(a.services.map((s) => s.key)).toEqual(['llama', 'lms', 'vllm']);
    expect(a.services[1]).toMatchObject({ status: 'ok', detail: 'nemotron-nano', canRestart: true });
    expect(a.services[2]).toMatchObject({ status: 'ok', detail: 'mixtral', canRestart: true });
  });

  it('no LMS/vLLM rows when no capable agent exists (agent_id null)', () => {
    const a = CView.actions({ ...full,
      lms: { agent_id: null, agent_online: false },
      vllm: { agent_id: null, agent_online: false } });
    expect(a.services.map((s) => s.key)).toEqual(['llama']);
    expect(a.models.map((r) => r.key)).toEqual(['llama']);
  });




  it('agentsKnown false when the agents read failed, true when it returned', () => {
    expect(CView.actions(full).agentsKnown).toBe(true);
    expect(CView.actions({ ...full, agents: null }).agentsKnown).toBe(false);
  });

  it('gated: admin reads null → llama row live, admin rows disabled, no autopilot/pending', () => {
    const a = CView.actions({ llama: full.llama, health: null, autopilot: null, agents: null,
                              version: 'v1', now: NOW });
    expect(a.gated).toBe(true);
    expect(a.services[0].canRestart).toBe(true);   // provider control stays live
    expect(a.autopilot).toBeNull();
    expect(a.pins).toEqual([]);
    expect(a.model.pinned).toBe(false);
  });

  it('empty world degrades, never throws', () => {
    const a = CView.actions({});
    expect(a.services).toHaveLength(1);
    expect(a.services[0].status).toBe('idle');
    expect(a.models).toHaveLength(1);
    expect(a.model.name).toBeNull();
    expect(a.model.resident).toBe(false);
  });
});

describe('CView.audit', () => {
  it('renders one line per entry with actor, outcome and age', () => {
    const rows = CView.audit([
      { ts: '2026-08-09T17:45:41.669823', actor: 'llmadmin', action: 'agent.approve',
        target: 'mac-mini-m4', outcome: 'ok' },
      { ts: '2026-08-09T17:40:00', actor: 'llmoperator', path: '/api/admin/agents',
        outcome: 'denied' },
    ], Date.parse('2026-08-09T19:45:41Z') / 1000);
    expect(rows[0]).toMatchObject({ name: 'agent.approve', ok: true });
    expect(rows[0].detail).toBe('llmadmin · ok · 2h ago → mac-mini-m4');
    expect(rows[1]).toMatchObject({ name: '/api/admin/agents', ok: false });
  });

  it('caps the list and never throws on junk', () => {
    expect(CView.audit(null)).toEqual([]);
    expect(CView.audit([{}])[0].name).toBe('action');
    expect(CView.audit(Array.from({ length: 90 }, () => ({})))).toHaveLength(40);
  });
});

describe('CView.devices', () => {
  const list = [
    { endpoint: 'https://web.push.apple.com/AAAA111122223333', created: NOW - 7200,
      ua: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605 Version/17.5 Mobile Safari/604' },
    { endpoint: 'https://fcm.googleapis.com/fcm/send/BBBB444455556666', created: NOW - 60,
      ua: 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537 Chrome/126 Mobile Safari/537' },
  ];

  it('names a device by OS and browser, not by its opaque endpoint', () => {
    const rows = CView.devices(list, null, NOW);
    expect(rows[0].name).toBe('iPhone · Safari');
    expect(rows[1].name).toBe('Android · Chrome');
  });

  it('shows an age and an endpoint tail so two identical clients differ', () => {
    const rows = CView.devices(list, null, NOW);
    expect(rows[0].detail).toBe('added 2h ago · …1122223333');
    expect(rows[1].detail).toBe('added 1m ago · …4455556666');
  });

  it('flags the calling device so an admin does not cut themselves off blind', () => {
    const rows = CView.devices(list, list[1].endpoint, NOW);
    expect(rows.map((r) => r.self)).toEqual([false, true]);
  });

  it('keeps the full endpoint, which is what removal is keyed on', () => {
    expect(CView.devices(list, null, NOW)[0].endpoint).toBe(list[0].endpoint);
  });

  it('degrades rather than throwing on junk', () => {
    expect(CView.devices(null, null, NOW)).toEqual([]);
    const [row] = CView.devices([{ endpoint: 'https://x.example/e' }], null, NOW);
    expect(row.name).toBe('unknown device');
  });

  it('Chrome on iOS reads as Chrome, not Safari', () => {
    // CriOS carries "Safari" in the UA too, so order matters.
    expect(CView.deviceLabel('Mozilla/5.0 (iPhone) CriOS/126 Mobile/15E148 Safari/604'))
      .toBe('iPhone · Chrome');
  });
});

describe('CView.powerCoverage', () => {
  const hosts = (n, withPower) => Array.from({ length: n },
    (_, i) => ({ hostname: 'h' + i, has_power: i < withPower }));

  it('flags a partial fleet, which is what makes a low cost explicable', () => {
    // Live symptom: energy read $0.02 all day because five of six hosts
    // reported no power at all, not because the arithmetic was wrong.
    const c = CView.powerCoverage({ hosts: hosts(6, 1) });
    expect(c).toMatchObject({ partial: true, with: 1, total: 6 });
    expect(c.text).toBe('1 of 6 hosts metered');
  });

  it('is quiet when every host is metered', () => {
    expect(CView.powerCoverage({ hosts: hosts(3, 3) }).partial).toBe(false);
  });

  it('does not claim a partial fleet when there are no hosts at all', () => {
    expect(CView.powerCoverage({}).partial).toBe(false);
    expect(CView.powerCoverage(null).partial).toBe(false);
  });

  it('surfaces on the Home energy tile', () => {
    const g = CView.glance({ energy: { totals: { cost_usd: 0.02, kwh: 0.148 },
      price_kwh: 0.15, hosts: hosts(6, 1) } });
    expect(g.power[1]).toMatchObject({ v: '$0.02' });
    expect(g.power[1].sub).toBe('148 Wh · 1 of 6 hosts metered');
  });

  it('shows the price instead once coverage is complete', () => {
    const g = CView.glance({ energy: { totals: { cost_usd: 1.84, kwh: 9.2 },
      price_kwh: 0.2, hosts: hosts(6, 6) } });
    expect(g.power[1].sub).toBe('9.2 kWh · $0.2/kWh');
  });
});
