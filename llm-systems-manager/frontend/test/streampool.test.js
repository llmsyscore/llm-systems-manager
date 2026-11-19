// #456: the stream-pool badge must reflect CURRENT pool state, not latch
// forever on the since-boot refusal counter.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import LMSeries, { trackRefusals } from '../js/lib/series.js';
import MV from '../js/lib/manager-view.js';
import OV from '../js/lib/overall-view.js';
import { srcFile, evalGlobal } from './helpers/harness.js';

evalGlobal(srcFile('kit/kit.js'));

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

// Runs the real fetchManagerStreamsCard() and the Manager page's kit panels with fetch stubbed (#1432).
describe('dashboard-manager badge wiring (#456)', () => {
  let run;
  const note = () => document.querySelector('[data-card="mgr-streams"] .p-hd .p-note');
  // The value cell of a Connection health entry.
  const cell = (label) => [...document.querySelectorAll('#mg-b-streams .r-lab')].find(d => d.textContent === label).parentElement.querySelector('.r-val');

  beforeEach(() => {
    document.body.innerHTML = '<div class="mg-root" id="mg-root"></div>';
    window._activeTab = 'dashboard';
    window._subTabState = { dashboard: 'manager' };
    window._ovPinned = () => false;
    window.mcShellStore = { getItem: () => null, setItem: () => {} };
    window.layout = {};
    window.LMSeries = LMSeries;
    window.MV = MV;
    window.OV = OV;
    evalGlobal(dashSrc);
    run = window.fetchManagerStreamsCard;
  });

  // Drives one poll cycle with a stubbed /api/admin/stream-stats response.
  async function poll(pool, extra = {}) {
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ pool, agents: [], ...extra }) });
    await run();
  }

  it('shows the open-route counters and warns on refusals (#1216)', async () => {
    await poll({ active: 1, limit: 10, peak: 2, refusals: 0 },
               { anon_requests_per_min: 3210, anon_refused_per_min: 2610, anon_budget_per_min: 600 });
    expect(cell('Open routes').textContent).toBe('3210 /min of 600');
    expect(cell('Refused (open routes)').textContent).toBe('2610');
    expect(cell('Refused (open routes)').parentElement.classList.contains('mg-over')).toBe(true);
    expect(note().textContent).toBe('flooded');
    expect(note().classList.contains('mg-warn')).toBe(true);
    await poll({ active: 1, limit: 10, peak: 2, refusals: 0 },
               { anon_requests_per_min: 12, anon_refused_per_min: 0, anon_budget_per_min: 600 });
    expect(note().textContent).toBe('ok');
    expect(note().classList.contains('mg-warn')).toBe(false);
    expect(cell('Open routes').textContent).toBe('12 /min of 600');
  });

  it('no longer derives saturation from the lifetime refusal counter', async () => {
    // active well under limit, but a huge lifetime refusal count — must stay non-crit.
    await poll({ active: 2, limit: 10, peak: 15, refusals: 500 });
    expect(note().textContent).not.toBe('saturated');
    expect(note().classList.contains('mg-bad')).toBe(false);
  });

  it('labels lifetime totals as since-boot', async () => {
    await poll({ active: 1, limit: 10, peak: 7, refusals: 2 });
    expect(cell('Peak (boot)').textContent).toBe('7');
    expect(cell('Refusals (boot)').textContent).toBe('2');
  });

  it('no longer crit-styles the since-boot peak cell when peak alone reaches the limit', async () => {
    await poll({ active: 1, limit: 10, peak: 10, refusals: 0 });
    expect(cell('Peak (boot)').parentElement.classList.contains('mg-over')).toBe(false);
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
    expect(note().textContent).toBe('refusing');
    expect(note().classList.contains('mg-warn')).toBe(true);
  });

  it('flags a host row when its own refusal count grew recently', async () => {
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
    const cells = [...document.querySelectorAll('#mg-b-hosts tbody tr td')].map(td => td.textContent);
    expect(cells[0]).toBe('h1');
    expect(cells).toContain('6 · recent');
  });

  // wiring (unexecutable): index.html loads both halves of the stream pool.
  it('index.html loads series.js and dashboard-manager.js', () => {
    expect(indexSrc).toContain('src="/static/js/lib/series.js"');
    expect(indexSrc).toContain('src="/static/js/dashboard-manager.js"');
  });
});
