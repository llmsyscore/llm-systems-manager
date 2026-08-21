// #589: hero bucket-size selector — 5m default, layout-persisted choice,
// local re-bucketing. Validation is pure (OV.heroBucketMs); the handler
// runs as real overall.js source in jsdom, per the wiring-test pattern.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import OV from '../js/lib/overall-view.js';

const here = dirname(fileURLToPath(import.meta.url));
const overall = readFileSync(join(here, '..', 'js', 'overall.js'), 'utf8');
const charts = readFileSync(join(here, '..', 'js', 'charts.js'), 'utf8');
const html = readFileSync(join(here, '..', 'index.html'), 'utf8');

describe('OV.heroBucketMs (#589)', () => {
  it('defaults to 5 minutes', () => {
    expect(OV.HERO_BUCKET_DEFAULT_MS).toBe(300000);
    expect(OV.heroBucketMs(undefined)).toBe(300000);
    expect(OV.heroBucketMs(null)).toBe(300000);
  });
  it('accepts each offered width, string or number', () => {
    for (const v of OV.HERO_BUCKET_CHOICES) {
      expect(OV.heroBucketMs(v)).toBe(v);
      expect(OV.heroBucketMs(String(v))).toBe(v);
    }
  });
  it('clamps junk to the default', () => {
    expect(OV.heroBucketMs('123456')).toBe(300000);
    expect(OV.heroBucketMs('bogus')).toBe(300000);
    expect(OV.heroBucketMs('')).toBe(300000);
  });
});

function fnSrc(name) {
  const m = overall.match(new RegExp(`function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  expect(m, `${name} not found in overall.js`).toBeTruthy();
  return m[0];
}

function loadHandlers() {
  document.body.innerHTML = `
    <select id="ovHeroBucket">
      <option value="60000">1m</option><option value="300000">5m</option>
      <option value="900000">15m</option><option value="3600000">1h</option>
    </select>`;
  window.OV = OV;
  (0, eval)([
    'let _ovHeroBucketMs = null;',
    fnSrc('ovHeroBucketMs'), fnSrc('ovHeroBucketSync'), fnSrc('ovHeroBucketChange'),
    'window.ovHeroBucketMs = ovHeroBucketMs;',
    'window.ovHeroBucketSync = ovHeroBucketSync;',
    'window.ovHeroBucketChange = ovHeroBucketChange;',
  ].join('\n'));
}

describe('hero bucket handlers (#589)', () => {
  beforeEach(() => {
    window._ovHeroRows = [{ ts: '2026-08-21T10:00:00Z' }];
    window._ovHeroRender = vi.fn();
    window.loadOverallHistory = vi.fn(() => Promise.resolve());
    loadHandlers();
  });

  it('every page load starts at the 5m default', () => {
    expect(window.ovHeroBucketMs()).toBe(300000);
    window.ovHeroBucketSync();
    expect(document.getElementById('ovHeroBucket').value).toBe('300000');
  });

  it('change applies for the session and re-buckets locally from cached rows', () => {
    const sel = document.getElementById('ovHeroBucket');
    sel.value = '60000';
    window.ovHeroBucketChange(sel);
    expect(window.ovHeroBucketMs()).toBe(60000);
    expect(sel.value).toBe('60000');
    expect(window._ovHeroRender).toHaveBeenCalledTimes(1);
    expect(window.loadOverallHistory).not.toHaveBeenCalled();
  });

  it('junk select values clamp to the default', () => {
    const sel = document.getElementById('ovHeroBucket');
    sel.value = '';
    window.ovHeroBucketChange(sel);
    expect(window.ovHeroBucketMs()).toBe(300000);
  });

  it('change without cached rows falls back to the history backfill', () => {
    window._ovHeroRows = null;
    const sel = document.getElementById('ovHeroBucket');
    sel.value = '900000';
    window.ovHeroBucketChange(sel);
    expect(window.loadOverallHistory).toHaveBeenCalledTimes(1);
  });
});

describe('hero bucket wiring (#589)', () => {
  it('index.html option values match OV.HERO_BUCKET_CHOICES', () => {
    const m = html.match(/<select id="ovHeroBucket"[\s\S]*?<\/select>/);
    expect(m).toBeTruthy();
    const values = [...m[0].matchAll(/<option value="(\d+)"/g)].map(x => Number(x[1]));
    expect(values).toEqual(OV.HERO_BUCKET_CHOICES);
    expect(m[0]).toContain('onchange="ovHeroBucketChange(this)"');
  });

  it('backfill re-buckets via the selected width with a generation guard', () => {
    expect(charts).toMatch(/OV\.heroSeries\(_ovHeroRows, bucketMs\)/);
    expect(charts).toMatch(/const bucketMs = ovHeroBucketMs\(\)/);
    expect(charts).toMatch(/gen !== _ovHistoryGen/);
  });

  it('live append uses the selected width', () => {
    expect(overall).toMatch(/ovHeroBucketMs\(\), 'max'/);
  });
});
