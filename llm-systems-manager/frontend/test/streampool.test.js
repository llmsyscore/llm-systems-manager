// #456: the stream-pool badge must reflect CURRENT pool state, not latch
// forever on the since-boot refusal counter.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { trackRefusals } from '../js/lib/series.js';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const dashSrc = readFileSync(join(root, 'js/dashboard-manager.js'), 'utf8');
const indexSrc = readFileSync(join(root, 'index.html'), 'utf8');

describe('trackRefusals', () => {
  it('does not report recent on first sample even with a nonzero lifetime count', () => {
    const t = trackRefusals(null, 72, 1000);
    expect(t.recent).toBe(false);
    expect(t.count).toBe(72);
  });

  it('reports recent when the counter grows between samples', () => {
    let t = trackRefusals(null, 72, 1000);
    t = trackRefusals(t, 75, 11000);
    expect(t.recent).toBe(true);
    expect(t.lastIncreaseMs).toBe(11000);
  });

  it('clears recent after the window elapses with a flat counter', () => {
    let t = trackRefusals(null, 72, 1000);
    t = trackRefusals(t, 75, 11000);
    t = trackRefusals(t, 75, 11000 + 60001);
    expect(t.recent).toBe(false);
  });

  it('stays recent while inside the window', () => {
    let t = trackRefusals(null, 0, 1000);
    t = trackRefusals(t, 3, 11000);
    t = trackRefusals(t, 3, 41000);
    expect(t.recent).toBe(true);
  });

  it('treats a counter reset (process restart) as not-refusing and adopts the new count', () => {
    let t = trackRefusals(null, 72, 1000);
    t = trackRefusals(t, 0, 11000);
    expect(t.recent).toBe(false);
    expect(t.count).toBe(0);
  });

  it('tolerates a missing counter without losing state', () => {
    let t = trackRefusals(null, 5, 1000);
    t = trackRefusals(t, undefined, 11000);
    expect(t.count).toBe(5);
    expect(t.recent).toBe(false);
    t = trackRefusals(t, 8, 21000);
    expect(t.recent).toBe(true);
  });

  it('honors a custom recency window', () => {
    let t = trackRefusals(null, 0, 0, 5000);
    t = trackRefusals(t, 1, 1000, 5000);
    expect(t.recent).toBe(true);
    t = trackRefusals(t, 1, 6001, 5000);
    expect(t.recent).toBe(false);
  });
});

describe('dashboard-manager badge wiring', () => {
  it('no longer derives saturation from the lifetime refusal counter', () => {
    expect(dashSrc).not.toMatch(/saturated\s*=\s*\(pool\.refusals\s*>\s*0\)/);
  });

  it('uses trackRefusals for pool and per-agent recency', () => {
    expect(dashSrc).toMatch(/LMSeries\.trackRefusals/);
  });

  it('labels lifetime totals as since-boot', () => {
    expect(dashSrc).toMatch(/Peak \(boot\)/);
    expect(dashSrc).toMatch(/Refusals \(boot\)/);
  });

  it('no longer crit-styles the since-boot peak cell', () => {
    expect(dashSrc).not.toMatch(/pool\.peak\s*>=\s*pool\.limit/);
  });

  it('has a warn badge state for recent refusals below the cap', () => {
    expect(dashSrc).toMatch(/status--warn/);
    expect(dashSrc).toMatch(/refusing/);
  });

  it('index.html cache-busts the touched scripts with a fresh version', () => {
    expect(indexSrc).toMatch(/js\/lib\/series\.js\?v=2026\.07\.20-1/);
    expect(indexSrc).toMatch(/js\/dashboard-manager\.js\?v=2026\.07\.20-1/);
  });
});
