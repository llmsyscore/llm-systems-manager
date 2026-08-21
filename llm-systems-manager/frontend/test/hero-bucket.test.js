// #589: hero bucket-size selector — default 5m, persisted choice, and
// re-bucketing on change. Runs the real charts.js declarations in jsdom.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const charts = readFileSync(join(here, '..', 'js', 'charts.js'), 'utf8');
const html = readFileSync(join(here, '..', 'index.html'), 'utf8');

function block(re) {
  const m = charts.match(re);
  expect(m, `${re} not found in charts.js`).toBeTruthy();
  return m[0];
}

// The bucket const/let declarations + the two ovHeroBucket* functions.
const SRC = [
  block(/const HERO_BUCKET_DEFAULT_MS[\s\S]*?let HERO_BUCKET_MS = \(\(\) => \{[\s\S]*?\}\)\(\);/),
  block(/function ovHeroBucketInit\([\s\S]*?\n\}/),
  block(/function ovHeroBucketChange\([\s\S]*?\n\}/),
  'window._getBucket = () => HERO_BUCKET_MS;',
  'window.ovHeroBucketInit = ovHeroBucketInit; window.ovHeroBucketChange = ovHeroBucketChange;',
].join('\n');

function loadWithDom() {
  document.body.innerHTML = `
    <select id="ovHeroBucket">
      <option value="60000">1m</option><option value="300000">5m</option>
      <option value="900000">15m</option><option value="3600000">1h</option>
    </select>`;
  (0, eval)(SRC);
  ovHeroBucketInit();
}

beforeEach(() => {
  localStorage.clear();
  window.loadOverallHistory = vi.fn(() => Promise.resolve());
});

describe('hero bucket selector (#589)', () => {
  it('defaults to 5 minutes', () => {
    loadWithDom();
    expect(window._getBucket()).toBe(300000);
    expect(document.getElementById('ovHeroBucket').value).toBe('300000');
  });

  it('restores a persisted choice and reflects it in the selector', () => {
    localStorage.setItem('ovHeroBucketMs', '900000');
    loadWithDom();
    expect(window._getBucket()).toBe(900000);
    expect(document.getElementById('ovHeroBucket').value).toBe('900000');
  });

  it('rejects junk in storage and falls back to the default', () => {
    localStorage.setItem('ovHeroBucketMs', '123456');
    loadWithDom();
    expect(window._getBucket()).toBe(300000);
  });

  it('change persists the value and re-buckets from history', () => {
    loadWithDom();
    document.getElementById('ovHeroBucket').value = '60000';
    ovHeroBucketChange();
    expect(window._getBucket()).toBe(60000);
    expect(localStorage.getItem('ovHeroBucketMs')).toBe('60000');
    expect(window.loadOverallHistory).toHaveBeenCalledTimes(1);
  });

  it('same-value change is a no-op', () => {
    loadWithDom();
    document.getElementById('ovHeroBucket').value = '300000';
    ovHeroBucketChange();
    expect(window.loadOverallHistory).not.toHaveBeenCalled();
  });

  it('index.html carries the selector with the 5m default option', () => {
    expect(html).toContain('id="ovHeroBucket"');
    expect(html).toMatch(/<option value="300000" selected>/);
    expect(html).toContain('onchange="ovHeroBucketChange()"');
  });

  it('backfill and live append both use HERO_BUCKET_MS', () => {
    expect(charts).toMatch(/OV\.heroSeries\(rows, HERO_BUCKET_MS\)/);
    const overall = readFileSync(join(here, '..', 'js', 'overall.js'), 'utf8');
    expect(overall).toMatch(/HERO_BUCKET_MS, 'max'/);
  });
});
