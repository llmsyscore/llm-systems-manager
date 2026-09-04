// #822: LivePause — per-browser-tab pause flag for scheduled pollers and
// stream frames; badge + drawer follow it, sessionStorage remembers it.
import { describe, test, expect } from 'vitest';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const foundationSrc = readFileSync(join(here, '..', 'js', 'foundation.js'), 'utf8');
const drawerSrc = readFileSync(join(here, '..', 'js', 'lib', 'settingsdrawer.js'), 'utf8');

function harness(storage) {
  const dom = new JSDOM('<!doctype html><html><head></head><body>'
    + '<button id="intervalBadge" class="hdr-badge"><span class="dot"></span><b>—</b></button>'
    + '<div id="settingsOverlay" class="open"><section id="sdRefresh"></section></div>'
    + '</body></html>', { runScripts: 'dangerously', url: 'http://localhost/' });
  const w = dom.window;
  w.fetch = async () => ({ ok: true, json: async () => ({}) });
  if (storage) { for (const [k, v] of Object.entries(storage)) w.sessionStorage.setItem(k, v); }
  const inject = (code) => { const s = w.document.createElement('script'); s.textContent = code; w.document.head.appendChild(s); };
  inject(drawerSrc);
  inject(foundationSrc);
  return w;
}

describe('LivePause', () => {
  test('every() skips ticks while paused and resumes after', () => {
    const w = harness();
    let ticks = 0;
    const saved = w.setInterval;
    let cb = null;
    w.setInterval = (fn) => { cb = fn; return 1; };
    w.LivePause.every(() => { ticks++; }, 1000);
    w.setInterval = saved;
    cb(); expect(ticks).toBe(1);
    w.LivePause.set(true);
    cb(); cb(); expect(ticks).toBe(1);
    w.LivePause.set(false);
    cb(); expect(ticks).toBe(2);
  });

  test('set() persists per tab and dispatches lsm:livepause once per change', () => {
    const w = harness();
    const seen = [];
    w.document.addEventListener('lsm:livepause', (e) => seen.push(e.detail.paused));
    w.LivePause.set(true); w.LivePause.set(true);
    expect(seen).toEqual([true]);
    expect(w.sessionStorage.getItem('lsm.livePaused')).toBe('1');
    w.LivePause.toggle();
    expect(seen).toEqual([true, false]);
    expect(w.sessionStorage.getItem('lsm.livePaused')).toBe(null);
  });

  test('a remembered pause survives reload', () => {
    const w = harness({ 'lsm.livePaused': '1' });
    expect(w.LivePause.on).toBe(true);
  });

  test('badge and drawer switch follow the flag', () => {
    const w = harness();
    w._pollCfg = { poll_interval: 5, interval_mode: 'auto' };
    w._renderIntervalBadge();
    const badge = w.document.getElementById('intervalBadge');
    expect(badge.className).toBe('hdr-badge auto');
    w.LivePause.set(true);
    expect(badge.className).toBe('hdr-badge paused');
    expect(badge.textContent).toContain('paused');
    const sw = w.document.querySelector('[data-sd="pause"]');
    expect(sw.getAttribute('aria-checked')).toBe('true');
    expect(w.document.querySelector('#sdRefresh .sd-live.paused')).not.toBeNull();
    // Clicking the switch (via the drawer's delegated handler) resumes.
    w._sdBind();
    sw.click();
    expect(w.LivePause.on).toBe(false);
    expect(badge.className).toBe('hdr-badge auto');
    expect(w.document.querySelector('[data-sd="pause"]').getAttribute('aria-checked')).toBe('false');
  });
});
