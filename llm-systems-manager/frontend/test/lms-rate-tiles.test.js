// #736, #1432: LM Studio Gen/Prompt tokens/s on the host page — live while active, the 60-min
// active-sample mean otherwise, the 60-min peak always. Real host-view.js tiles/details with real LMPeaks trackers.
import { describe, it, expect } from 'vitest';
import HP from '../js/lib/host-view.js';
import LMPeaks from '../js/lib/peaks.js';

const T0 = Date.UTC(2026, 7, 29, 20, 0, 0);
const MIN = 60000;
const AVG = '60‑min avg';
// A rate with no value reads a dash alone, with no mode or peak.
const DASH_ONLY = '—';

// A page's tracker pair, fed the way js/hostpage.js feeds it (one push per live sample).
function page() {
  const peaks = { tps: LMPeaks.makeTracker(LMPeaks.RATE_WINDOW_MS), pps: LMPeaks.makeTracker(LMPeaks.RATE_WINDOW_MS) };
  const sample = (gen, prompt, online = true) => ({ agent_online: online, gateway_rates: gen == null ? undefined : { gen_tps: gen, prompt_tps: prompt } });
  return {
    peaks,
    push(at, gen, prompt) { peaks.tps.push(at, gen); peaks.pps.push(at, prompt); },
    // [Gen text, Prompt text] from the Server details panel, and the Gen tokens/s tile.
    read(at, gen, prompt, online = true) {
      const live = sample(gen, prompt, online), inf = { live, peaks, now: at };
      const d = HP.details('lms', 'details', HP.normalize(live, { page: 'lms' }), inf, online);
      const tile = HP.tiles('lms', HP.normalize(live, { page: 'lms' }), inf, online).find(t => t.label === 'Gen tokens/s');
      return { gen: d.find(r => r[0] === 'Gen tokens/s')[1], prompt: d.find(r => r[0] === 'Prompt tokens/s')[1], tile };
    },
  };
}

describe('LM Studio rate lines (#736)', () => {
  it('shows the live rate with its mode and peak while traffic flows', () => {
    const p = page();
    p.push(T0, 42.5, 900);
    const r = p.read(T0, 42.5, 900);
    expect(r.gen).toBe('42.5 · live · peak 42.5 · 0s ago');
    expect(r.prompt).toBe('900.0 · live · peak 900.0 · 0s ago');
    expect(r.tile).toMatchObject({ value: '42.5', sub: 'live · peak 42.5 · 0s ago' });
  });

  it('switches to the 60-min average of active samples when the rate drops to 0', () => {
    const p = page();
    p.push(T0, 10, 100);
    p.push(T0 + 15000, 30, 300);
    p.push(T0 + 30000, 0, 0);
    const r = p.read(T0 + 30000, 0, 0);
    expect(r.gen).toBe(`20.0 · ${AVG} · peak 30.0 · 15s ago`);
    expect(r.prompt).toBe(`200.0 · ${AVG} · peak 300.0 · 15s ago`);
    expect(r.tile).toMatchObject({ value: '20.0', sub: `${AVG} · peak 30.0 · 15s ago` });
  });

  it('gen and prompt are independent: gen idle while prompt is live', () => {
    const p = page();
    p.push(T0, 0, 500);
    const r = p.read(T0, 0, 500);
    expect(r.gen).toBe(DASH_ONLY);
    expect(r.prompt).toBe('500.0 · live · peak 500.0 · 0s ago');
  });

  it('samples age out after 60 minutes; no data at all reads —', () => {
    const p = page();
    p.push(T0, 50, 50);
    p.push(T0 + 61 * MIN, 0, 0);
    expect(p.read(T0 + 61 * MIN, 0, 0).gen).toBe(DASH_ONLY);
    expect(p.read(T0 + 62 * MIN, null, null).prompt).toBe(DASH_ONLY);
  });

  it('no gateway sample at all falls back to the window average', () => {
    const p = page();
    p.push(T0, 40, 400);
    expect(p.read(T0 + 15000, null, null).gen).toBe(`40.0 · ${AVG} · peak 40.0 · 15s ago`);
  });

  it('an offline host shows dashes, not its last rates', () => {
    const p = page();
    p.push(T0, 40, 400);
    const r = p.read(T0 + 15000, 40, 400, false);
    expect(r.gen).toBe('—');
    expect(r.prompt).toBe('—');
    expect(r.tile.value).toBe('—');
  });

  it('history rows seed the window through the row clock and reset clears it', () => {
    const p = page();
    const rows = [{ ts: new Date(T0 - 90 * MIN).toISOString(), lms_tps: 999, lms_pps: 999 },
      { ts: new Date(T0 - 10 * MIN).toISOString(), lms_tps: 80, lms_pps: 0 },
      { ts: new Date(T0 - 5 * MIN).toISOString(), lms_tps: 0, lms_pps: 640 }];
    const clock = LMPeaks.rowClock(rows, T0);
    rows.forEach(r => { p.peaks.tps.push(clock(r.ts), r.lms_tps); p.peaks.pps.push(clock(r.ts), r.lms_pps); });
    const r = p.read(T0, 0, 0);
    expect(r.gen).toBe(`80.0 · ${AVG} · peak 80.0 · 10m ago`);
    expect(r.prompt).toBe(`640.0 · ${AVG} · peak 640.0 · 5m ago`);
    p.peaks.tps.reset();
    expect(p.read(T0 + 15000, 0, 0).gen).toBe(DASH_ONLY);
  });

  it('a server window wins over the browser tracker', () => {
    const p = page();
    p.push(T0, 5, 5);
    const live = { agent_online: true, gateway_rates: { gen_tps: 0, prompt_tps: 0 },
      throughput_window: { gen: { avg: 33, peak: { v: 70, ts: new Date(T0 - 2 * MIN).toISOString() } } } };
    const d = HP.details('lms', 'details', HP.normalize(live, { page: 'lms' }), { live, peaks: p.peaks, now: T0 }, true);
    expect(d.find(r => r[0] === 'Gen tokens/s')[1]).toBe(`33.0 · ${AVG} · peak 70.0 · 2m ago`);
  });
});
