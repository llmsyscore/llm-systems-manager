// #456: the stream-pool badge must reflect CURRENT pool state, not latch
// forever on the since-boot refusal counter.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { trackRefusals } from '../js/lib/series.js';
import { srcFile, fnSrc, evalGlobal } from './helpers/harness.js';

const dashSrc = srcFile('js/dashboard-manager.js');
const indexSrc = srcFile('index.html');

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

// Runs the real fetchManagerStreamsCard() with fetch/LMSeries/_dashSetStatus stubbed.
describe('dashboard-manager badge wiring (#456)', () => {
  let run;

  beforeEach(() => {
    document.body.innerHTML = `
      <div id="mgrStreamsSummary"></div>
      <table><tbody id="mgrStreamsTable"></tbody></table>
      <span id="mgrStreamsBadge"></span>`;
    window._activeTab = 'manager';
    window._subTabState = {};
    window._ovPinned = () => false;
    window._dashSetStatus = () => {};
    window._mgrPoolRefusalTrack = null;
    window._mgrAgentRefusalTrack = {};
    // Real trackRefusals, stubbed sub-tab gate (the gate itself isn't under test here).
    window.LMSeries = { trackRefusals, isManagerSubActive: () => true };
    const fn = fnSrc(dashSrc, 'fetchManagerStreamsCard');
    expect(fn, 'fetchManagerStreamsCard not found').toBeTruthy();
    evalGlobal(fn + '\nwindow.fetchManagerStreamsCard = fetchManagerStreamsCard;');
    run = window.fetchManagerStreamsCard;
  });

  // Drives one poll cycle with a stubbed /api/admin/stream-stats response.
  async function poll(pool, extra = {}) {
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ pool, agents: [], ...extra }) });
    await run();
  }

  it('no longer derives saturation from the lifetime refusal counter', async () => {
    // active well under limit, but a huge lifetime refusal count — must stay non-crit.
    await poll({ active: 2, limit: 10, peak: 15, refusals: 500 });
    const badge = document.getElementById('mgrStreamsBadge');
    expect(badge.className).not.toContain('status--crit');
  });

  it('labels lifetime totals as since-boot', async () => {
    await poll({ active: 1, limit: 10, peak: 7, refusals: 2 });
    const html = document.getElementById('mgrStreamsSummary').innerHTML;
    expect(html).toContain('Peak (boot)');
    expect(html).toContain('Refusals (boot)');
  });

  it('no longer crit-styles the since-boot peak cell when peak alone reaches the limit', async () => {
    await poll({ active: 1, limit: 10, peak: 10, refusals: 0 });
    const html = document.getElementById('mgrStreamsSummary').innerHTML;
    const idx = html.indexOf('Peak (boot)');
    const chunk = html.slice(idx, html.indexOf('</div>', idx));
    expect(chunk).not.toContain('crit');
  });

  it('has a warn badge state for recent refusals below the cap', async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(1000);
      await poll({ active: 2, limit: 10, peak: 5, refusals: 5 }); // baseline sample
      vi.setSystemTime(11000);
      await poll({ active: 2, limit: 10, peak: 5, refusals: 8 }); // grew inside the window
    } finally {
      vi.useRealTimers();
    }
    const badge = document.getElementById('mgrStreamsBadge');
    expect(badge.className).toContain('status--warn');
    expect(badge.innerHTML).toContain('refusing');
  });

  it('flags an agent row when its own refusal count grew recently', async () => {
    const agent = (refusals) => ([{
      hostname: 'h1', agent_id: 'a1', reachable: true,
      active: 1, cap: 5, peak: 2, refusals, terminal_sessions: 0,
    }]);
    vi.useFakeTimers();
    try {
      vi.setSystemTime(1000);
      await poll({ active: 0, limit: 10, peak: 0, refusals: 0 }, { agents: agent(3) });
      vi.setSystemTime(11000);
      await poll({ active: 0, limit: 10, peak: 0, refusals: 0 }, { agents: agent(6) });
    } finally {
      vi.useRealTimers();
    }
    const rowHtml = document.getElementById('mgrStreamsTable').innerHTML;
    expect(rowHtml).toContain('var(--crit');
  });

  // wiring (unexecutable): asserts a version-string pairing between two files,
  // not code the harness can meaningfully "execute".
  it('index.html cache-busts the touched scripts with a fresh version', () => {
    expect(indexSrc).toMatch(/js\/lib\/series\.js\?v=2026\.08\.04-2/);
    expect(indexSrc).toMatch(/js\/dashboard-manager\.js\?v=2026\.08\.17-1/);
  });
});
