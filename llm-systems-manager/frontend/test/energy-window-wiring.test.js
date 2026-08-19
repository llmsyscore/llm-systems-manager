// #575: window-param wiring — tz only for local-anchored kinds, and an
// incomplete custom range never fires a defaulted fetch. Real sources in jsdom.
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'js', 'energy.js'), 'utf8');

function fnSrc(name) {
  const m = src.match(new RegExp(
    `function ${name}\\([^)]*\\) \\{[\\s\\S]*?\\n\\}`));
  expect(m, `${name} not found in energy.js`).toBeTruthy();
  return m[0];
}

beforeEach(() => {
  document.body.innerHTML = `
    <select id="enWindow"></select>
    <input id="enCustomFrom"><input id="enCustomTo">
    <div id="enNote"></div>`;
  window.EN = require('../js/lib/energy.js');
  window.fetch = vi.fn(() => new Promise(() => {}));
  (0, eval)([
    fnSrc('_enEl'), fnSrc('_enNote'), fnSrc('_enWindowParams'),
    fnSrc('_enParams'), fnSrc('enRefresh'),
    'window._enWindowParams = _enWindowParams; window.enRefresh = enRefresh;',
  ].join('\n'));
});

function _setWindow(v) {
  const sel = document.getElementById('enWindow');
  sel.innerHTML = `<option value="${v}" selected>${v}</option>`;
  sel.value = v;
}

describe('tz_offset_min gating (#575)', () => {
  it('trailing-day windows stay tz-free', () => {
    _setWindow('days:7');
    expect(window._enWindowParams().has('tz_offset_min')).toBe(false);
  });
  it('today carries the tz offset', () => {
    _setWindow('today');
    const p = window._enWindowParams();
    expect(p.get('days')).toBe('1');
    expect(p.has('tz_offset_min')).toBe(true);
  });
  it('ytd and complete custom carry the tz offset', () => {
    _setWindow('ytd');
    expect(window._enWindowParams().has('tz_offset_min')).toBe(true);
    _setWindow('custom');
    document.getElementById('enCustomFrom').value = '2026-06-01';
    document.getElementById('enCustomTo').value = '2026-06-10';
    const p = window._enWindowParams();
    expect(p.get('start')).toBe('2026-06-01');
    expect(p.has('tz_offset_min')).toBe(true);
  });
});

describe('incomplete custom range (#575)', () => {
  it('enRefresh refuses to fetch and asks for both dates', () => {
    _setWindow('custom');
    document.getElementById('enCustomFrom').value = '2026-06-01';
    window.enRefresh();
    expect(window.fetch).not.toHaveBeenCalled();
    expect(document.getElementById('enNote').textContent).toMatch(/both dates/i);
  });
});
