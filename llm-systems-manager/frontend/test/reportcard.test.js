import { describe, it, expect } from 'vitest';
import RC from '../js/lib/reportcard.js';

const RESULT = {
  model: 'qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf',
  ttft_s: 0.42, prefill_tps: 1219.0, gen_tps: 43.7,
  tokens_per_joule: 0.21, usd_per_mtok: 0.19, avg_watts: 208.0,
  power_source: 'psu', gpu_config: '7900 XTX',
  vram_total_mb: 24560, vram_used_mb: 21000,
};

const CARD = {
  ts: 1753600000, provider: 'llama', mode: 'standard',
  preset_version: 'preset_v1', eligible: true, result: RESULT,
};

const COND_A = { hardware: { gpu: '7900 XTX', driver: 'amdgpu 6.12.0' },
  backend: { name: 'llama.cpp', build: 'b1234', server_cmd: '/opt/models/llama-server -m /opt/models/x.gguf' } };

describe('submitUrl', () => {
  it('targets the leaderboard issue form with the JSON prefilled', () => {
    const u = new URL(RC.submitUrl(CARD));
    expect(u.pathname.endsWith('/issues/new')).toBe(true);
    expect(u.searchParams.get('template')).toBe('submit.yml');
    const json = JSON.parse(u.searchParams.get('card-json'));
    expect(json.result.gen_tps).toBe(43.7);
  });

  it('never includes agent identity', () => {
    const withId = { ...CARD, agent_id: 'deadbeefdeadbeef' };
    const url = RC.submitUrl(withId);
    expect(url).not.toContain('deadbeef');
    expect(JSON.parse(new URL(url).searchParams.get('card-json')).agent_id)
      .toBeUndefined();
  });

  it('returns empty string for a non-eligible card', () => {
    expect(RC.submitUrl({ ...CARD, eligible: false })).toBe('');
    expect(RC.submitUrl({ ...CARD, mode: 'custom' })).toBe('');
  });

  it('never includes the live section', () => {
    const withLive = { ...CARD, result: { ...RESULT, live: { bench: 'throughput_1k', decode_tps: 50 } } };
    const url = RC.submitUrl(withLive);
    const json = JSON.parse(new URL(url).searchParams.get('card-json'));
    expect(json.result.live).toBeUndefined();
    expect(json.result.gen_tps).toBe(43.7);
  });

  it('never includes the run conditions', () => {
    const url = RC.submitUrl({ ...CARD, result: { ...RESULT, conditions: COND_A } });
    expect(url).not.toContain('llama-server');
    expect(JSON.parse(new URL(url).searchParams.get('card-json')).result.conditions).toBeUndefined();
  });
});

describe('buildCard', () => {
  it('renders headline numbers', () => {
    const frag = RC.buildCard(RESULT);
    expect(frag.textContent).toContain('43.7');
    expect(frag.textContent).toContain('7900 XTX');
  });

  it('shows the no-telemetry note only when power data is absent', () => {
    const noP = RC.buildCard({ ...RESULT, tokens_per_joule: null,
      usd_per_mtok: null, avg_watts: null });
    expect(noP.querySelector('.rc-energy').classList.contains('rc-muted'))
      .toBe(true);
    expect(noP.textContent).toContain('no power telemetry');
    expect(RC.buildCard(RESULT).querySelector('.rc-energy')).toBeNull();
  });

  it('renders the six measurement cells including energy figures', () => {
    const text = RC.buildCard(RESULT).textContent;
    expect(text).toContain('prompt proc');
    expect(text).toContain('first token');
    expect(text).toContain('420');             // 0.42 s → 420 ms
    expect(text).toContain('power avg (wall)');
    expect(text).toContain('per 1k tok');
    expect(text).toContain('1.32');            // 1000 / 0.21 / 3600 Wh
    expect(text).toContain('$0.19');
  });

  it('labels each power source, including Apple SoC watts', () => {
    expect(RC.buildCard(RESULT).textContent).toContain('power avg (wall)');
    expect(RC.buildCard({ ...RESULT, power_source: 'gpu' }).textContent)
      .toContain('power avg (GPU)');
    expect(RC.buildCard({ ...RESULT, power_source: 'mac' }).textContent)
      .toContain('power avg (SoC)');
  });

  it('omits the source label when the source is unknown', () => {
    const text = RC.buildCard({ ...RESULT, power_source: null }).textContent;
    expect(text).toContain('power avg');
    expect(text).not.toContain('power avg (');
  });

  it('carries an export-excluded save button on the card', () => {
    const btn = RC.buildCard(RESULT).querySelector('.rc-savebtn');
    expect(btn).toBeTruthy();
    expect(btn.classList.contains('rc-nox')).toBe(true);
    expect(btn.getAttribute('type')).toBe('button');
  });

  it('escapes provider-supplied strings rather than injecting markup', () => {
    const frag = RC.buildCard({ ...RESULT,
      gpu_config: '<img src=x onerror=alert(1)>' });
    expect(frag.querySelector('img')).toBeNull();
    expect(frag.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('falls back cleanly on missing fields', () => {
    const frag = RC.buildCard({});
    expect(frag.textContent).toContain('Unknown GPU');
    expect(frag.textContent).toContain('—');
  });
});

describe('live block (#885)', () => {
  const live = { run_id: 'r1', ts: '2026-09-08T20:36:50+00:00', bench: 'throughput_1k', decode_tps: 64.2, prefill_tps: 399,
    latency_s: 12, accept_rate: 0.5, wh_per_ktok: 3.1, energy_source: 'psu',
    levels: [{ concurrency: 1, pred_tps: 64.2, agg_pred_tps: 64.2 }, { concurrency: 2, pred_tps: 58, agg_pred_tps: 110 }],
    matrix: { benches: ['throughput_1k', 'throughput_32k'], osls: [256], cells: [{ bench: 'throughput_1k', osl: 256, pred_tps: 64.2 }, { bench: 'throughput_32k', osl: 256, pred_tps: 56.3 }] } };
  it('renders a Live block with four cells, the under-load strip and the matrix strip', () => {
    const frag = RC.buildCard({ gen_tps: 70, model: 'm', live });
    const blk = frag.querySelector('.rc-live');
    expect(blk).toBeTruthy();
    expect(blk.querySelectorAll('.rc-cell').length).toBe(4);
    expect(blk.textContent).toContain('64.2'); expect(blk.textContent).toContain('12'); expect(blk.textContent).toContain('50');
    expect(blk.querySelector('.rc-live-load').textContent).toBe('under load · 1: 64.2 · 2: 58.0 t/s');
    expect(blk.querySelector('.rc-live-matrix').textContent).toBe('prompt × output · 1k 64.2 · 32k 56.3 t/s (osl 256)');
  });
  it('omits the block without live data and omits strips without levels/matrix', () => {
    expect(RC.buildCard({ gen_tps: 70 }).querySelector('.rc-live')).toBeNull();
    const frag = RC.buildCard({ gen_tps: 70, live: { ...live, levels: [], matrix: null } });
    expect(frag.querySelector('.rc-live-load')).toBeNull(); expect(frag.querySelector('.rc-live-matrix')).toBeNull();
  });
  it('liveSummary is pure', () => {
    expect(RC.liveSummary({ levels: [{ concurrency: 1, pred_tps: 10 }], matrix: null }).loadText).toBe('under load · 1: 10.0 t/s');
    expect(RC.liveSummary({}).loadText).toBe('');
  });
});

describe('spread ranges (#1301)', () => {
  it('shows the range under the hero and in the cells when spread is present', () => {
    const sp = { gen_tps: { n: 10, min: 40, p25: 42, p50: 43.7, p75: 45, max: 48 },
                 prefill_tps: { n: 10, min: 1100, p25: 1180, p50: 1219, p75: 1250, max: 1300 },
                 ttft_s: { n: 10, min: 0.40, p25: 0.41, p50: 0.42, p75: 0.44, max: 0.5 } };
    const card = RC.buildCard({ ...RESULT, spread: sp });
    expect(card.querySelector('.rc-hero-range').textContent).toBe('42.0–45.0 tok/s · 10 reps');
    const ranges = [...card.querySelectorAll('.rc-range')].map(e => e.textContent);
    expect(ranges).toEqual(['1180–1250', '410–440']);
    expect(RC.buildCard(RESULT).querySelector('.rc-hero-range')).toBeNull();
    expect(RC.buildCard(RESULT).querySelectorAll('.rc-range').length).toBe(0);
  });
  it('liveSummary appends the decode range', () => {
    const live = { levels: [{ concurrency: 1, pred_tps: 50 }], spread: { decode_tps: { n: 8, min: 48, p25: 49, p50: 50, p75: 51, max: 52 } } };
    expect(RC.liveSummary(live).loadText).toBe('under load · 1: 50.0 (49.0–51.0) t/s');
  });
});

describe('conditions (#1302)', () => {
  it('buildCard appends the conditions block only when present', () => {
    const frag = RC.buildCard({ ...RESULT, conditions: COND_A });
    expect(frag.querySelector('details.cond-block')).not.toBeNull();
    expect(RC.buildCard(RESULT).querySelector('details.cond-block')).toBeNull();
  });
  it('places the conditions block before the Save button', () => {
    const root = RC.buildCard({ ...RESULT, conditions: COND_A }).querySelector('.rc-card');
    const kids = [...root.children];
    expect(kids.indexOf(root.querySelector('details.cond-block'))).toBe(kids.indexOf(root.querySelector('.rc-savebtn')) - 1);
  });
  it('cardPseudoRun maps a card into the comparison shape', () => {
    const p = RC.cardPseudoRun({ ...CARD, result: { ...RESULT, conditions: COND_A } });
    expect(p).toMatchObject({ tool: 'reportcard', model_id: RESULT.model, ok: true,
      summary: { gen_tps: 43.7, ppt_tps: 1219.0, ttft_ms: 420, mode: 'standard', preset_version: 'preset_v1', avg_watts: 208.0 } });
    expect(p.ts).toBe(new Date(CARD.ts * 1000).toISOString());
    expect(p.summary.wh_per_ktok).toBeCloseTo(1000 / 0.21 / 3600, 6);
    expect(p.conditions).toBe(COND_A);
  });
  it('cardPseudoRun is null-safe on missing figures', () => {
    const p = RC.cardPseudoRun({ ts: 1, result: {} });
    expect(p.summary.ttft_ms).toBeNull();
    expect(p.summary.wh_per_ktok).toBeNull();
  });
});

describe('trendSeries', () => {
  it('maps cards to chart arrays in time order', () => {
    const s = RC.trendSeries([
      { ts: 2, result: { gen_tps: 50, prefill_tps: 1000, tokens_per_joule: 0.2 } },
      { ts: 1, result: { gen_tps: 40, prefill_tps: 900, tokens_per_joule: 0.1 } }]);
    expect(s.gen).toEqual([40, 50]);
    expect(s.prefill).toEqual([900, 1000]);
    expect(s.tpj).toEqual([0.1, 0.2]);
    expect(s.labels.length).toBe(2);
  });

  it('handles an empty history', () => {
    const s = RC.trendSeries([]);
    expect(s.gen).toEqual([]);
    expect(s.labels).toEqual([]);
  });
});

describe('formatters', () => {
  it('renders an em dash for null and trims precision', () => {
    expect(RC.fmt(null)).toBe('—');
    expect(RC.fmt(43.6789)).toBe('43.7');
    expect(RC.fmtMb(null)).toBe('—');
    expect(RC.fmtMb(24560)).toBe('24.0 GB');
  });
});
