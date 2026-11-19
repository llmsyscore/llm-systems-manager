// #575: window-param wiring — tz only for local-anchored kinds, and an
// incomplete custom range never fires a defaulted fetch. Real sources in jsdom, driven through the Energy page head.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import OV from '../js/lib/overall-view.js';
import EN from '../js/lib/energy.js';
import { srcFile, evalGlobal, flush } from './helpers/harness.js';

const energy = srcFile('js/energy.js');
evalGlobal(srcFile('kit/kit.js'));
window.OV = OV;
window.EN = EN;

let calls;
beforeEach(() => {
  document.body.innerHTML = '<div class="en-root" id="en-root"></div>';
  window.layout = {};
  calls = [];
  window.fetch = vi.fn((url) => { calls.push(url); return new Promise(() => {}); });
  evalGlobal(energy);
  window.initEnergyTab();
});

// Picks a window in the head and returns the window summary URL's params it fetched, or null when none.
function pick(v, from, to) {
  calls.length = 0;
  document.getElementById('en-from').value = from || '';
  document.getElementById('en-to').value = to || '';
  window.document.getElementById('en-window').value = v;
  window.enOnControlChange();
  const url = calls.find(u => u.startsWith('/api/energy/summary?') && u !== '/api/energy/summary?days=1');
  return url ? new URLSearchParams(url.split('?')[1]) : null;
}

describe('tz_offset_min gating (#575)', () => {
  it('trailing-day windows stay tz-free', () => {
    expect(pick('days:7').has('tz_offset_min')).toBe(false);
  });
  it('today carries the tz offset', () => {
    const p = pick('today');
    expect(p.get('days')).toBe('1');
    expect(p.has('tz_offset_min')).toBe(true);
  });
  it('ytd and complete custom carry the tz offset', () => {
    expect(pick('ytd').has('tz_offset_min')).toBe(true);
    const p = pick('custom', '2026-06-01', '2026-06-10');
    expect(p.get('start')).toBe('2026-06-01');
    expect(p.has('tz_offset_min')).toBe(true);
  });
});

describe('incomplete custom range (#575)', () => {
  it('enRefresh refuses to fetch and asks for both dates', async () => {
    expect(pick('custom', '2026-06-01')).toBeNull();
    await flush();
    expect(calls).toHaveLength(0);
    expect(document.getElementById('en-note').textContent).toMatch(/both dates/i);
  });
});
