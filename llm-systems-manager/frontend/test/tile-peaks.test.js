// #591 glue: real overall.js seeding/push functions in jsdom — field-name
// mapping, lazy once-per-backfill seeding, and the OV.tiles peaks shape.
import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import LMPeaks from '../js/lib/peaks.js';

const here = dirname(fileURLToPath(import.meta.url));
const overall = readFileSync(join(here, '..', 'js', 'overall.js'), 'utf8');

function block(re) {
  const m = overall.match(re);
  expect(m, `${re} not found in overall.js`).toBeTruthy();
  return m[0];
}

beforeEach(() => {
  window.LMPeaks = LMPeaks;
  window._ovHeroRows = null;
  (0, eval)([
    block(/const _OV_TILE_PEAK_WINDOW_MS[\s\S]*?\n\}\);/),
    'let _ovTileSeedRows = null;',
    block(/function ovSeedTilePeaks\([\s\S]*?\n\}/),
    block(/function _ovTilePeaksPush\([\s\S]*?\n\}/),
    'window._T = { seed: ovSeedTilePeaks, push: _ovTilePeaksPush, trackers: _ovTilePeaks };',
  ].join('\n'));
});

describe('tile peak glue (#591)', () => {
  it('seeds every provider from the fleet-history field names', () => {
    const now = Date.now();
    const rows = [{ ts: new Date(now - 60000).toISOString(),
                    llama_tps: 51, llama_pps: 452, lms_tps: 7, lms_pps: 30,
                    vllm_tps: 12, vllm_pps: 3 },
                  { ts: new Date(now).toISOString() }];
    window._T.seed(rows);
    expect(window._T.trackers.llama.gen.peak(now).v).toBe(51);
    expect(window._T.trackers.llama.prompt.peak(now).v).toBe(452);
    expect(window._T.trackers.lms.gen.peak(now).v).toBe(7);
    expect(window._T.trackers.vllm.gen.peak(now).v).toBe(12);
    expect(window._T.trackers.vllm.prompt.peak(now).v).toBe(3);
  });

  it('rows outside the 60-min window are not seeded', () => {
    const now = Date.now();
    const rows = [{ ts: new Date(now - 70 * 60000).toISOString(), llama_tps: 99 },
                  { ts: new Date(now).toISOString(), llama_tps: 1 }];
    window._T.seed(rows);
    expect(window._T.trackers.llama.gen.peak(now).v).toBe(1);
  });

  it('push seeds lazily once per backfill and returns the rates shape', () => {
    const now = Date.now();
    window._ovHeroRows = [{ ts: new Date(now - 30000).toISOString(), lms_tps: 88 }];
    const agg = { throughput: { total_tps: 2.5, total_pps: 1.0 } };
    const peaks = window._T.push(null, agg, null);
    expect(peaks.lms.gen).toEqual({ v: 2.5, mode: 'live', peak: { v: 88, t: expect.any(Number) } });
    expect(peaks.lms.prompt.v).toBe(1.0);
    expect(peaks.llama.gen).toEqual({ v: null, mode: LMPeaks.AVG_LABEL, peak: null });
    const idle = window._T.push(null, { throughput: { total_tps: 0, total_pps: 0 } }, null);
    expect(idle.lms.gen.mode).toBe(LMPeaks.AVG_LABEL);
    expect(idle.lms.gen.v).toBeCloseTo((88 + 2.5) / 2);
    expect(peaks.vllm.gen.v).toBeNull();
    // Same rows object again: no re-seed, the seeded peak stays the peak.
    const again = window._T.push(null, agg, null);
    expect(again.lms.gen).toEqual({ v: 2.5, mode: 'live', peak: { v: 88, t: expect.any(Number) } });
  });
});
