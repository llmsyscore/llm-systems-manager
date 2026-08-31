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
